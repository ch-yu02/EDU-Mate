import {
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { EmptyState } from "./EmptyState";
import {
  analyzeVisualImage,
  sendRealtimeEvent,
  sessionImageUrl,
  uploadSessionImage,
} from "../services/api";
import type { GlobalSearchSourceRef } from "../types/agent";
import type { ImageCapture, LectureSession } from "../types/classroom";
import { formatClassTime } from "../utils/time";

const VISUAL_AUTO_RETRY_LIMIT = 2;
const VISUAL_AUTO_RETRY_DELAY_MS = 4000;
const IMAGE_VIEWER_MIN_ZOOM = 1;
const IMAGE_VIEWER_MAX_ZOOM = 4;
const IMAGE_VIEWER_ZOOM_STEP = 0.25;

// 图片 / OCR / VLM 面板，包含浏览器摄像头预览与手动拍照入口。
type VisualOcrPanelProps = {
  visuals: ImageCapture[];
  focusedSource?: GlobalSearchSourceRef | null;
  session?: LectureSession | null;
  onStatusMessage?: (message: string) => void;
};

export function VisualOcrPanel({
  visuals,
  focusedSource,
  session,
  onStatusMessage,
}: VisualOcrPanelProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const localPreviewUrlsRef = useRef<Record<string, string>>({});
  const sessionRef = useRef<LectureSession | null | undefined>(session);
  const visualAnalysisRetryCountsRef = useRef<Record<string, number>>({});
  const visualAnalysisRetryTimersRef = useRef<
    Record<string, ReturnType<typeof window.setTimeout>>
  >({});
  const runVisualAnalysisRef = useRef<
    | ((
        sessionId: string,
        imageId: string,
        options?: { force?: boolean },
      ) => Promise<void>)
    | null
  >(null);
  const imageViewerDragRef = useRef<ImageViewerDragState | null>(null);
  const [isCameraStarting, setIsCameraStarting] = useState(false);
  const [isCapturing, setIsCapturing] = useState(false);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [isCameraActive, setIsCameraActive] = useState(false);
  const [selectedVisualId, setSelectedVisualId] = useState<string | null>(null);
  const [isImageViewerOpen, setIsImageViewerOpen] = useState(false);
  const [imageViewerZoom, setImageViewerZoom] = useState(1);
  const [imageViewerPan, setImageViewerPan] = useState({ x: 0, y: 0 });
  const [localVisuals, setLocalVisuals] = useState<ImageCapture[]>([]);
  const [localPreviewUrls, setLocalPreviewUrls] = useState<Record<string, string>>({});
  const [failedImageIds, setFailedImageIds] = useState<Record<string, boolean>>({});
  const [analyzingImageIds, setAnalyzingImageIds] = useState<Record<string, boolean>>({});

  const isRecording = session?.status === "recording";
  const displayVisuals = useMemo(
    () => mergeVisuals(visuals, localVisuals),
    [localVisuals, visuals],
  );
  const orderedVisuals = useMemo(
    () => [...displayVisuals].sort((left, right) => left.capture_ts - right.capture_ts),
    [displayVisuals],
  );
  const selectedVisual = selectedVisualId
    ? displayVisuals.find((visual) => visual.image_id === selectedVisualId) ?? null
    : null;
  const selectedImageSrc = selectedVisual
    ? imageSourceForVisual(selectedVisual, localPreviewUrls, session?.session_id)
    : selectedVisualId
      ? localPreviewUrls[selectedVisualId] ?? null
      : null;
  const selectedImageTitle = selectedVisual?.caption || selectedVisual?.image_type || "课堂图片";
  const selectedIsAnalyzing = selectedVisualId ? !!analyzingImageIds[selectedVisualId] : false;
  const selectedImageIndex = selectedVisualId
    ? orderedVisuals.findIndex((visual) => visual.image_id === selectedVisualId)
    : -1;
  const selectedImagePosition =
    selectedImageIndex >= 0 ? `${selectedImageIndex + 1}/${orderedVisuals.length}` : null;

  const resetImageViewerTransform = useCallback(() => {
    imageViewerDragRef.current = null;
    setImageViewerZoom(1);
    setImageViewerPan({ x: 0, y: 0 });
  }, []);

  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  useEffect(() => {
    if (focusedSource?.type !== "visual") {
      return;
    }

    setSelectedVisualId(focusedSource.id);
  }, [focusedSource]);

  useEffect(() => {
    setSelectedVisualId(null);
    setIsImageViewerOpen(false);
    resetImageViewerTransform();
    setLocalVisuals([]);
    setFailedImageIds({});
    setLocalPreviewUrls({});
    Object.values(localPreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
    localPreviewUrlsRef.current = {};
    Object.values(visualAnalysisRetryTimersRef.current).forEach((timerId) =>
      window.clearTimeout(timerId),
    );
    visualAnalysisRetryTimersRef.current = {};
    visualAnalysisRetryCountsRef.current = {};
  }, [resetImageViewerTransform, session?.session_id]);

  const stopCamera = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
    setIsCameraActive(false);
  }, []);

  useEffect(() => {
    return () => {
      stopCamera();
      Object.values(localPreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
      localPreviewUrlsRef.current = {};
      Object.values(visualAnalysisRetryTimersRef.current).forEach((timerId) =>
        window.clearTimeout(timerId),
      );
      visualAnalysisRetryTimersRef.current = {};
    };
  }, [stopCamera]);

  useEffect(() => {
    if (isRecording) {
      return;
    }
    Object.values(visualAnalysisRetryTimersRef.current).forEach((timerId) =>
      window.clearTimeout(timerId),
    );
    visualAnalysisRetryTimersRef.current = {};
    visualAnalysisRetryCountsRef.current = {};
  }, [isRecording]);

  const clearVisualAnalysisRetry = useCallback((imageId: string) => {
    const retryTimer = visualAnalysisRetryTimersRef.current[imageId];
    if (retryTimer) {
      window.clearTimeout(retryTimer);
      delete visualAnalysisRetryTimersRef.current[imageId];
    }
    delete visualAnalysisRetryCountsRef.current[imageId];
  }, []);

  const selectViewerVisualByOffset = useCallback(
    (offset: number) => {
      if (!orderedVisuals.length) {
        return;
      }

      const currentIndex = selectedImageIndex >= 0 ? selectedImageIndex : orderedVisuals.length - 1;
      const nextIndex = (currentIndex + offset + orderedVisuals.length) % orderedVisuals.length;
      setSelectedVisualId(orderedVisuals[nextIndex].image_id);
      resetImageViewerTransform();
    },
    [orderedVisuals, resetImageViewerTransform, selectedImageIndex],
  );

  const openImageViewer = useCallback(() => {
    const targetVisual =
      selectedVisual ??
      orderedVisuals.find((visual) => visual.image_id === selectedVisualId) ??
      orderedVisuals[orderedVisuals.length - 1];
    if (!targetVisual) {
      onStatusMessage?.("当前还没有可查看的课堂图片。");
      return;
    }

    setSelectedVisualId(targetVisual.image_id);
    resetImageViewerTransform();
    setIsImageViewerOpen(true);
  }, [onStatusMessage, orderedVisuals, resetImageViewerTransform, selectedVisual, selectedVisualId]);

  const closeImageViewer = useCallback(() => {
    setIsImageViewerOpen(false);
    imageViewerDragRef.current = null;
  }, []);

  const updateImageViewerZoom = useCallback((delta: number) => {
    setImageViewerZoom((currentZoom) => {
      const nextZoom = clampNumber(
        currentZoom + delta,
        IMAGE_VIEWER_MIN_ZOOM,
        IMAGE_VIEWER_MAX_ZOOM,
      );
      if (nextZoom <= IMAGE_VIEWER_MIN_ZOOM) {
        setImageViewerPan({ x: 0, y: 0 });
      }
      return nextZoom;
    });
  }, []);

  const handleImageViewerWheel = useCallback(
    (event: ReactWheelEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      updateImageViewerZoom(event.deltaY < 0 ? IMAGE_VIEWER_ZOOM_STEP : -IMAGE_VIEWER_ZOOM_STEP);
    },
    [updateImageViewerZoom],
  );

  useEffect(() => {
    if (!isImageViewerOpen) {
      return;
    }

    const previousBodyOverflow = document.body.style.overflow;
    const previousRootOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.documentElement.style.overflow = previousRootOverflow;
    };
  }, [isImageViewerOpen]);

  const handleImageViewerPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (!isImageViewerOpen) {
        return;
      }

      imageViewerDragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: imageViewerPan.x,
        originY: imageViewerPan.y,
        moved: false,
      };
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    [imageViewerPan, isImageViewerOpen],
  );

  const handleImageViewerPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const dragState = imageViewerDragRef.current;
      if (!dragState || dragState.pointerId !== event.pointerId) {
        return;
      }

      const deltaX = event.clientX - dragState.startX;
      const deltaY = event.clientY - dragState.startY;
      dragState.moved = dragState.moved || Math.abs(deltaX) > 4 || Math.abs(deltaY) > 4;
      if (imageViewerZoom > IMAGE_VIEWER_MIN_ZOOM) {
        setImageViewerPan({
          x: dragState.originX + deltaX,
          y: dragState.originY + deltaY,
        });
      }
    },
    [imageViewerZoom],
  );

  const finishImageViewerPointerGesture = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const dragState = imageViewerDragRef.current;
      if (!dragState || dragState.pointerId !== event.pointerId) {
        return;
      }

      const deltaX = event.clientX - dragState.startX;
      const deltaY = event.clientY - dragState.startY;
      imageViewerDragRef.current = null;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }

      if (
        imageViewerZoom <= IMAGE_VIEWER_MIN_ZOOM &&
        Math.abs(deltaX) > 70 &&
        Math.abs(deltaY) < 90
      ) {
        selectViewerVisualByOffset(deltaX < 0 ? 1 : -1);
      }
    },
    [imageViewerZoom, selectViewerVisualByOffset],
  );

  useEffect(() => {
    if (!isImageViewerOpen) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeImageViewer();
        return;
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        selectViewerVisualByOffset(-1);
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        selectViewerVisualByOffset(1);
        return;
      }
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        updateImageViewerZoom(IMAGE_VIEWER_ZOOM_STEP);
        return;
      }
      if (event.key === "-") {
        event.preventDefault();
        updateImageViewerZoom(-IMAGE_VIEWER_ZOOM_STEP);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    closeImageViewer,
    isImageViewerOpen,
    selectViewerVisualByOffset,
    updateImageViewerZoom,
  ]);

  const scheduleVisualAnalysisRetry = useCallback(
    (sessionId: string, imageId: string, reason: string): boolean => {
      const activeSession = sessionRef.current;
      if (
        !activeSession ||
        activeSession.session_id !== sessionId ||
        activeSession.status !== "recording"
      ) {
        return false;
      }

      const retryCount = visualAnalysisRetryCountsRef.current[imageId] ?? 0;
      if (retryCount >= VISUAL_AUTO_RETRY_LIMIT) {
        return false;
      }

      const nextRetryCount = retryCount + 1;
      visualAnalysisRetryCountsRef.current[imageId] = nextRetryCount;
      const existingTimer = visualAnalysisRetryTimersRef.current[imageId];
      if (existingTimer) {
        window.clearTimeout(existingTimer);
      }

      setLocalVisuals((current) =>
        updateVisualById(current, imageId, {
          status: "processing",
          caption: `多模态分析失败：${reason}。${Math.round(
            VISUAL_AUTO_RETRY_DELAY_MS / 1000,
          )} 秒后自动重试（${nextRetryCount}/${VISUAL_AUTO_RETRY_LIMIT}）。`,
        }),
      );
      onStatusMessage?.(
        `照片分析失败，正在自动重试（${nextRetryCount}/${VISUAL_AUTO_RETRY_LIMIT}）：${reason}`,
      );

      visualAnalysisRetryTimersRef.current[imageId] = window.setTimeout(() => {
        delete visualAnalysisRetryTimersRef.current[imageId];
        const currentSession = sessionRef.current;
        if (
          !currentSession ||
          currentSession.session_id !== sessionId ||
          currentSession.status !== "recording"
        ) {
          return;
        }

        setLocalVisuals((current) =>
          updateVisualById(current, imageId, {
            status: "processing",
            caption: `正在第 ${nextRetryCount} 次自动重新分析。`,
          }),
        );
        void runVisualAnalysisRef.current?.(sessionId, imageId, {
          force: true,
        });
      }, VISUAL_AUTO_RETRY_DELAY_MS);

      return true;
    },
    [onStatusMessage],
  );

  const markImageFailed = useCallback((imageId: string) => {
    setFailedImageIds((current) => ({
      ...current,
      [imageId]: true,
    }));
  }, []);

  const markImageLoaded = useCallback((imageId: string) => {
    setFailedImageIds((current) => {
      if (!current[imageId]) {
        return current;
      }
      const next = { ...current };
      delete next[imageId];
      return next;
    });
  }, []);

  const removeLocalPreview = useCallback((imageId: string) => {
    const url = localPreviewUrlsRef.current[imageId];
    if (url) {
      URL.revokeObjectURL(url);
      delete localPreviewUrlsRef.current[imageId];
    }
    setLocalPreviewUrls((current) => {
      if (!current[imageId]) {
        return current;
      }
      const next = { ...current };
      delete next[imageId];
      return next;
    });
  }, []);

  const removeLocalVisual = useCallback((imageId: string) => {
    setLocalVisuals((current) => current.filter((visual) => visual.image_id !== imageId));
  }, []);

  const runVisualAnalysis = useCallback(
    async (
      sessionId: string,
      imageId: string,
      options: { force?: boolean } = {},
    ) => {
      setAnalyzingImageIds((current) => ({
        ...current,
        [imageId]: true,
      }));
      try {
        const analysis = await analyzeVisualImage(sessionId, imageId, {
          force: options.force ?? false,
        });
        if (analysis.status === "failed") {
          const reason = analysis.warnings.join("；") || "多模态分析失败。";
          if (!scheduleVisualAnalysisRetry(sessionId, imageId, reason)) {
            setLocalVisuals((current) =>
              updateVisualById(current, imageId, {
                status: "failed",
                caption: reason,
              }),
            );
            onStatusMessage?.(`照片已保存，但多模态分析失败：${reason}`);
          }
          return;
        }
        clearVisualAnalysisRetry(imageId);
        setLocalVisuals((current) =>
          updateVisualById(current, imageId, {
            status: "processed",
            caption: analysis.caption,
            visual_text: analysis.visual_text,
            key_points: analysis.key_points,
          }),
        );
        onStatusMessage?.(
          `照片分析完成，图谱更新 ${analysis.graph_patch_operations} 项。`,
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : "多模态分析失败。";
        if (!scheduleVisualAnalysisRetry(sessionId, imageId, message)) {
          setLocalVisuals((current) =>
            updateVisualById(current, imageId, {
              status: "failed",
              caption: message,
            }),
          );
          onStatusMessage?.(`照片已保存，但多模态分析失败：${message}`);
        }
      } finally {
        setAnalyzingImageIds((current) => {
          const next = { ...current };
          delete next[imageId];
          return next;
        });
      }
    },
    [clearVisualAnalysisRetry, onStatusMessage, scheduleVisualAnalysisRetry],
  );

  useEffect(() => {
    runVisualAnalysisRef.current = runVisualAnalysis;
  }, [runVisualAnalysis]);

  async function startCamera() {
    if (!navigator.mediaDevices?.getUserMedia) {
      setCameraError("当前浏览器不支持摄像头访问。");
      return;
    }
    setIsCameraStarting(true);
    setCameraError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play();
      }
      setIsCameraActive(true);
      onStatusMessage?.("摄像头已开启，可点击拍照或按 Ctrl+1。");
    } catch (error) {
      setCameraError(error instanceof Error ? error.message : "摄像头启动失败。");
    } finally {
      setIsCameraStarting(false);
    }
  }

  const capturePhoto = useCallback(async () => {
    if (!session || session.status !== "recording") {
      onStatusMessage?.("请先开始或接入一节录制中的课堂。");
      return;
    }
    const video = videoRef.current;
    if (!video || !streamRef.current || video.readyState < 2) {
      onStatusMessage?.("摄像头还没有准备好。");
      return;
    }

    setIsCapturing(true);
    setCameraError(null);
    const imageId = cameraImageId();
    try {
      const blob = await videoFrameBlob(video);
      const localPreviewUrl = URL.createObjectURL(blob);
      localPreviewUrlsRef.current[imageId] = localPreviewUrl;
      setLocalPreviewUrls((current) => ({
        ...current,
        [imageId]: localPreviewUrl,
      }));
      setSelectedVisualId(null);
      const captureTs = sessionRelativeSeconds(session);
      const draftVisual: ImageCapture = {
        image_id: imageId,
        session_id: session.session_id,
        capture_ts: captureTs,
        image_path: `local://sessions/${session.session_id}/images/${imageId}.jpg`,
        source: "browser_camera",
        image_type: "camera_snapshot",
        status: "processing",
        caption: "图片已保存，正在交给云端多模态模型分析。",
      };
      setLocalVisuals((current) => upsertVisual(current, draftVisual));

      const upload = await uploadSessionImage(session.session_id, imageId, blob);
      const visualPayload: ImageCapture = {
        ...draftVisual,
        image_path: upload.image_path,
      };
      setLocalVisuals((current) => upsertVisual(current, visualPayload));
      await sendRealtimeEvent({
        session_id: session.session_id,
        event_type: "image.capture",
        payload: { ...visualPayload },
      });
      onStatusMessage?.("照片已保存，多模态分析已在后台进行。");
      void runVisualAnalysis(session.session_id, imageId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "拍照上传失败。";
      removeLocalPreview(imageId);
      removeLocalVisual(imageId);
      setCameraError(message);
      onStatusMessage?.(message);
    } finally {
      setIsCapturing(false);
    }
  }, [onStatusMessage, removeLocalPreview, removeLocalVisual, runVisualAnalysis, session]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (!(event.ctrlKey && event.key === "1")) {
        return;
      }
      event.preventDefault();
      void capturePhoto();
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [capturePhoto]);

  return (
    <section className="panel visual-panel" aria-labelledby="visual-title">
      <div className="panel-header">
        <div>
          <h2 id="visual-title">图片 / 视觉</h2>
          <span>{displayVisuals.length} 张</span>
        </div>
      </div>

      <div className="camera-capture-panel" aria-label="摄像头拍照">
        {displayVisuals.length ? (
          <div className="camera-image-picker" aria-label="课堂图片选择">
            <button
              className={!selectedVisualId ? "active" : ""}
              type="button"
              onClick={() => setSelectedVisualId(null)}
            >
              实时画面
            </button>
            {[...displayVisuals].reverse().map((visual) => (
              <button
                className={selectedVisualId === visual.image_id ? "active" : ""}
                key={visual.image_id}
                title={visual.caption || visual.image_type || visual.image_id}
                type="button"
                onClick={() => setSelectedVisualId(visual.image_id)}
              >
                <ClassroomImage
                  alt={visual.caption || visual.image_type || "课堂图片"}
                  className="camera-picker-image"
                  failed={!!failedImageIds[visual.image_id]}
                  fallbackLabel="图片"
                  imageId={visual.image_id}
                  onError={markImageFailed}
                  onLoad={markImageLoaded}
                  src={imageSourceForVisual(visual, localPreviewUrls, session?.session_id)}
                />
                <span>{formatClassTime(visual.capture_ts)}</span>
                {analyzingImageIds[visual.image_id] ? <small>分析中</small> : null}
              </button>
            ))}
          </div>
        ) : null}
        <div className="camera-display-frame">
          <video
            aria-label="实时摄像头预览"
            className={selectedImageSrc ? "hidden-camera-feed" : ""}
            muted
            playsInline
            ref={videoRef}
          />
          {selectedImageSrc && selectedVisualId ? (
            <ClassroomImage
              alt={selectedImageTitle}
              className="camera-selected-image"
              failed={!!failedImageIds[selectedVisualId]}
              fallbackLabel="图片暂不可见"
              imageId={selectedVisualId}
              onError={markImageFailed}
              onLoad={markImageLoaded}
              src={selectedImageSrc}
            />
          ) : null}
        </div>
        <div className="camera-controls">
          <button
            disabled={!isRecording || isCameraStarting}
            type="button"
            onClick={() => {
              if (isCameraActive) {
                stopCamera();
                return;
              }
              void startCamera();
            }}
          >
            {isCameraActive ? "停止摄像头" : isCameraStarting ? "启动中" : "开启摄像头"}
          </button>
          <button
            disabled={!isRecording || !isCameraActive || isCapturing}
            type="button"
            onClick={() => void capturePhoto()}
          >
            {isCapturing ? "保存中" : "拍照"}
          </button>
          <button
            disabled={!displayVisuals.length}
            type="button"
            onClick={openImageViewer}
          >
            全屏查看
          </button>
          <span>快捷键 Ctrl+1</span>
        </div>
        {cameraError ? <p className="camera-error">{cameraError}</p> : null}
      </div>

      <VisualAnalysisDetail
        isAnalyzing={selectedIsAnalyzing}
        isFocused={focusedSource?.type === "visual" && focusedSource.id === selectedVisualId}
        onOpenImageViewer={openImageViewer}
        selectedVisualId={selectedVisualId}
        visual={selectedVisual}
      />
      {isImageViewerOpen && selectedVisual && selectedImageSrc ? (
        <ImageViewerOverlay
          failed={!!failedImageIds[selectedVisual.image_id]}
          imagePosition={selectedImagePosition}
          onClose={closeImageViewer}
          onNext={() => selectViewerVisualByOffset(1)}
          onPointerCancel={finishImageViewerPointerGesture}
          onPointerDown={handleImageViewerPointerDown}
          onPointerMove={handleImageViewerPointerMove}
          onPointerUp={finishImageViewerPointerGesture}
          onPrevious={() => selectViewerVisualByOffset(-1)}
          onReset={resetImageViewerTransform}
          onWheel={handleImageViewerWheel}
          onZoomIn={() => updateImageViewerZoom(IMAGE_VIEWER_ZOOM_STEP)}
          onZoomOut={() => updateImageViewerZoom(-IMAGE_VIEWER_ZOOM_STEP)}
          pan={imageViewerPan}
          src={selectedImageSrc}
          title={selectedImageTitle}
          visual={selectedVisual}
          zoom={imageViewerZoom}
        />
      ) : null}
    </section>
  );
}

type VisualAnalysisDetailProps = {
  isAnalyzing: boolean;
  isFocused: boolean;
  onOpenImageViewer: () => void;
  selectedVisualId: string | null;
  visual: ImageCapture | null;
};

function VisualAnalysisDetail({
  isAnalyzing,
  isFocused,
  onOpenImageViewer,
  selectedVisualId,
  visual,
}: VisualAnalysisDetailProps) {
  if (!selectedVisualId) {
    return (
      <div className="visual-analysis-detail visual-analysis-empty">
        <EmptyState label="选择课堂图片查看分析" />
      </div>
    );
  }

  if (!visual) {
    return (
      <div className="visual-analysis-detail visual-analysis-empty">
        <EmptyState label="正在加载图片分析" />
      </div>
    );
  }

  const hasAnalysis =
    !!visual.ocr_text ||
    !!visual.caption ||
    !!visual.visual_text?.length ||
    !!visual.key_points?.length;

  return (
    <article className={`visual-analysis-detail ${isFocused ? "focused-source" : ""}`}>
      <div className="visual-detail-header">
        <div>
          <strong>{visual.image_type || "课堂图片"}</strong>
          <span>{formatClassTime(visual.capture_ts)}</span>
        </div>
        <div className="visual-detail-actions">
          {isAnalyzing || visual.status === "processing" ? (
            <span className="visual-analysis-status">分析中</span>
          ) : null}
          <button
            className="visual-fullscreen-button"
            type="button"
            onClick={onOpenImageViewer}
          >
            全屏
          </button>
        </div>
      </div>
      <dl className="visual-meta visual-detail-meta">
        <div>
          <dt>来源</dt>
          <dd>{visual.source || "-"}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{visual.status}</dd>
        </div>
      </dl>

      {!hasAnalysis ? (
        <div className="visual-analysis-placeholder">
          {isAnalyzing || visual.status === "processing"
            ? "图片已保存，等待多模态分析结果。"
            : "当前图片暂无分析内容。"}
        </div>
      ) : null}
      {visual.caption ? (
        <div className="visual-block">
          <span>描述</span>
          <p>{visual.caption}</p>
        </div>
      ) : null}
      {visual.key_points?.length ? (
        <div className="visual-block">
          <span>图片要点</span>
          <ul>
            {visual.key_points.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {visual.visual_text?.length ? (
        <div className="visual-block">
          <span>视觉文字</span>
          <ul>
            {visual.visual_text.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {visual.ocr_text ? (
        <div className="visual-block">
          <span>OCR</span>
          <p>{visual.ocr_text}</p>
        </div>
      ) : null}
      <code className="image-path">{visual.image_path}</code>
    </article>
  );
}

type ImageViewerDragState = {
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
  moved: boolean;
};

type ImageViewerOverlayProps = {
  failed: boolean;
  imagePosition: string | null;
  onClose: () => void;
  onNext: () => void;
  onPointerCancel: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onPointerMove: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onPrevious: () => void;
  onReset: () => void;
  onWheel: (event: ReactWheelEvent<HTMLDivElement>) => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  pan: { x: number; y: number };
  src: string;
  title: string;
  visual: ImageCapture;
  zoom: number;
};

function ImageViewerOverlay({
  failed,
  imagePosition,
  onClose,
  onNext,
  onPointerCancel,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPrevious,
  onReset,
  onWheel,
  onZoomIn,
  onZoomOut,
  pan,
  src,
  title,
  visual,
  zoom,
}: ImageViewerOverlayProps) {
  return (
    <div
      className="image-viewer-overlay"
      role="dialog"
      aria-modal="true"
      onWheel={onWheel}
    >
      <div className="image-viewer-topbar">
        <div>
          <strong>{visual.image_type || "课堂图片"}</strong>
          <span>
            {formatClassTime(visual.capture_ts)}
            {imagePosition ? ` · ${imagePosition}` : ""}
          </span>
        </div>
        <div className="image-viewer-actions">
          <button type="button" onClick={onZoomOut} title="缩小" aria-label="缩小">
            -
          </button>
          <button type="button" onClick={onReset} title="重置缩放" aria-label="重置缩放">
            {Math.round(zoom * 100)}%
          </button>
          <button type="button" onClick={onZoomIn} title="放大" aria-label="放大">
            +
          </button>
          <button type="button" onClick={onClose} title="关闭" aria-label="关闭">
            x
          </button>
        </div>
      </div>
      <div
        className={`image-viewer-stage ${
          zoom > IMAGE_VIEWER_MIN_ZOOM ? "is-pannable" : "is-swipeable"
        }`}
        onPointerCancel={onPointerCancel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      >
        <button
          className="image-viewer-nav image-viewer-nav-left"
          type="button"
          onClick={onPrevious}
          title="上一张"
          aria-label="上一张"
        >
          &lt;
        </button>
        {failed ? (
          <div className="image-viewer-fallback">图片暂不可见</div>
        ) : (
          <img
            alt={title}
            draggable={false}
            src={src}
            style={{
              transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
            }}
          />
        )}
        <button
          className="image-viewer-nav image-viewer-nav-right"
          type="button"
          onClick={onNext}
          title="下一张"
          aria-label="下一张"
        >
          &gt;
        </button>
      </div>
      {visual.caption ? <p className="image-viewer-caption">{visual.caption}</p> : null}
    </div>
  );
}

type ClassroomImageProps = {
  alt: string;
  className: string;
  failed: boolean;
  fallbackLabel: string;
  imageId: string;
  loading?: "lazy" | "eager";
  onError: (imageId: string) => void;
  onLoad: (imageId: string) => void;
  src: string;
};

function ClassroomImage({
  alt,
  className,
  failed,
  fallbackLabel,
  imageId,
  loading,
  onError,
  onLoad,
  src,
}: ClassroomImageProps) {
  if (!src || failed) {
    return (
      <div className={`${className} image-load-fallback`} role="img" aria-label={alt}>
        {fallbackLabel}
      </div>
    );
  }

  return (
    <img
      alt={alt}
      className={className}
      loading={loading}
      onError={() => onError(imageId)}
      onLoad={() => onLoad(imageId)}
      src={src}
    />
  );
}

function imageSourceForVisual(
  visual: ImageCapture,
  localPreviewUrls: Record<string, string>,
  sessionId?: string,
): string {
  return localPreviewUrls[visual.image_id] ?? sessionImageUrl(visual, sessionId);
}

function mergeVisuals(serverVisuals: ImageCapture[], localVisuals: ImageCapture[]): ImageCapture[] {
  return [...localVisuals, ...serverVisuals].reduce(
    (merged, visual) => upsertVisual(merged, visual),
    [] as ImageCapture[],
  );
}

function upsertVisual(visuals: ImageCapture[], nextVisual: ImageCapture): ImageCapture[] {
  const index = visuals.findIndex((visual) => visual.image_id === nextVisual.image_id);
  if (index < 0) {
    return [...visuals, nextVisual];
  }

  return visuals.map((visual, visualIndex) =>
    visualIndex === index ? mergeVisual(visual, nextVisual) : visual,
  );
}

function mergeVisual(current: ImageCapture, nextVisual: ImageCapture): ImageCapture {
  if (current.status !== "processing" && nextVisual.status === "processing") {
    return {
      ...nextVisual,
      ...current,
    };
  }

  return {
    ...current,
    ...nextVisual,
  };
}

function updateVisualById(
  visuals: ImageCapture[],
  imageId: string,
  updates: Partial<ImageCapture>,
): ImageCapture[] {
  return visuals.map((visual) =>
    visual.image_id === imageId
      ? {
          ...visual,
          ...updates,
        }
      : visual,
  );
}

function cameraImageId(): string {
  const random = Math.random().toString(16).slice(2, 8);
  return `img_camera_${Date.now()}_${random}`;
}

function sessionRelativeSeconds(session: LectureSession): number {
  const startedAt = Date.parse(session.start_time);
  if (!Number.isFinite(startedAt)) {
    return 0;
  }
  return Math.max(0, (Date.now() - startedAt) / 1000);
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function videoFrameBlob(video: HTMLVideoElement): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth || 1280;
  canvas.height = video.videoHeight || 720;
  const context = canvas.getContext("2d");
  if (!context) {
    return Promise.reject(new Error("无法创建拍照画布。"));
  }
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          reject(new Error("无法生成照片数据。"));
          return;
        }
        resolve(blob);
      },
      "image/jpeg",
      0.92,
    );
  });
}
