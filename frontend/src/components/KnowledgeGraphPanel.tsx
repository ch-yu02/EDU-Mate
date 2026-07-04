import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { EmptyState } from "./EmptyState";
import type { GlobalSearchSourceRef } from "../types/agent";
import type {
  KnowledgeEdge,
  KnowledgeGraphView,
  KnowledgeGraphStatus,
  KnowledgeNode,
  ImageCapture,
  SourceRef,
  TranscriptSegment,
} from "../types/classroom";

// 知识图谱面板。
//
// 后端 KnowledgeGraphManager 通过 graph_patch.operations 推送增量变更。
// 面板提供两种显示：
// 1. 列表视图：稳定、信息密度高，适合快速确认节点和边是否正确进入前端。
// 2. 图形视图：用轻量 SVG 画出节点和连线，不引入额外图库，避免增加依赖风险。
//
// 后续如果接 React Flow / Cytoscape，可以继续使用 KnowledgeGraphView 作为
// 数据适配层，把这里的 SVG 视图替换为专业图谱组件。
type KnowledgeGraphPanelProps = {
  graph: KnowledgeGraphView;
  focusedSource?: GlobalSearchSourceRef | null;
  status?: KnowledgeGraphStatus;
  transcript?: TranscriptSegment[];
  visuals?: ImageCapture[];
};

type GraphViewMode = "list" | "graph";

export function KnowledgeGraphPanel({
  graph,
  focusedSource,
  status = "live",
  transcript = [],
  visuals = [],
}: KnowledgeGraphPanelProps) {
  // 用户可以在“列表”和“图形”之间切换。
  // 默认列表，因为任何节点/边都能直接读出来。
  const [viewMode, setViewMode] = useState<GraphViewMode>("list");

  // nodeById 用于把后端边里的 source/target 节点 ID 转成可读 label。
  // 后端 KnowledgeEdge.source/target 保存的是 node_id，不是实体中文名。
  const nodeById = useMemo(() => {
    return new Map(graph.nodes.map((node) => [node.node_id, node]));
  }, [graph.nodes]);

  return (
    <section
      className={`panel graph-panel ${viewMode === "graph" ? "graph-panel-graphic" : ""}`}
      aria-labelledby="graph-title"
    >
      <div className="panel-header">
        <div>
          <h2 id="graph-title">知识图谱</h2>
          <span>{knowledgeGraphStatusLabel(status, graph.version)}</span>
        </div>
        <strong>{graph.nodes.length}</strong>
      </div>

      {graph.nodes.length === 0 ? (
        <EmptyState label="等待知识点" />
      ) : (
        <div className={`graph-content ${viewMode === "graph" ? "graph-content-graphic" : ""}`}>
          <div
            className="segmented-control graph-view-toggle"
            role="tablist"
            aria-label="知识图谱视图"
          >
            <button
              className={viewMode === "list" ? "active" : ""}
              type="button"
              role="tab"
              aria-selected={viewMode === "list"}
              onClick={() => setViewMode("list")}
            >
              列表
            </button>
            <button
              className={viewMode === "graph" ? "active" : ""}
              type="button"
              role="tab"
              aria-selected={viewMode === "graph"}
              onClick={() => setViewMode("graph")}
            >
              图形
            </button>
          </div>

          {viewMode === "list" ? (
            <KnowledgeGraphList
              focusedSource={focusedSource}
              graph={graph}
              nodeById={nodeById}
              transcript={transcript}
              visuals={visuals}
            />
          ) : (
            <KnowledgeGraphCanvas
              focusedSource={focusedSource}
              graph={graph}
              nodeById={nodeById}
            />
          )}
        </div>
      )}
    </section>
  );
}

function knowledgeGraphStatusLabel(status: KnowledgeGraphStatus, version: number): string {
  if (status === "final") {
    return "final";
  }
  if (status === "finalizing") {
    return "finalizing";
  }
  if (status === "failed") {
    return "final failed";
  }
  return `v${version}`;
}

type KnowledgeGraphListProps = {
  focusedSource?: GlobalSearchSourceRef | null;
  graph: KnowledgeGraphView;
  nodeById: Map<string, KnowledgeNode>;
  transcript: TranscriptSegment[];
  visuals: ImageCapture[];
};

function KnowledgeGraphList({
  focusedSource,
  graph,
  nodeById,
  transcript,
  visuals,
}: KnowledgeGraphListProps) {
  const itemRefs = useRef<Record<string, HTMLElement | null>>({});
  const focusedId = focusedGraphId(focusedSource);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    if (!focusedId) {
      return;
    }

    itemRefs.current[focusedId]?.scrollIntoView({
      block: "center",
      behavior: "smooth",
    });
  }, [focusedId]);

  return (
    <div className="graph-list-view">
      {/* 节点区：展示概念名称、类型、重要度和摘要。 */}
      <div className="node-list">
        {graph.nodes.map((node) => (
          <article
            className={`node-item ${focusedId === node.node_id ? "focused-source" : ""}`}
            key={node.node_id}
            ref={(element) => {
              itemRefs.current[node.node_id] = element;
            }}
          >
            <div className="node-title-row">
              <h3>{node.label}</h3>
              <span>{node.type || "concept"}</span>
            </div>
            <p>{node.summary || "暂无摘要"}</p>
            <SourceRefToggle
              expanded={expandedId === node.node_id}
              id={node.node_id}
              refs={node.source_refs ?? []}
              transcript={transcript}
              visuals={visuals}
              onToggle={setExpandedId}
            />
            {typeof node.importance === "number" ? (
              <meter min="0" max="1" value={node.importance}>
                {node.importance}
              </meter>
            ) : null}
          </article>
        ))}
      </div>

      {/* 关系区：展示 source --relation--> target。 */}
      <div className="edge-list" aria-label="知识关系">
        {graph.edges.length === 0 ? (
          <div className="edge-empty">等待关系</div>
        ) : (
          graph.edges.map((edge) => (
            <div
              className={`edge-item ${focusedId === edge.edge_id ? "focused-source" : ""}`}
              key={edge.edge_id}
              ref={(element) => {
                itemRefs.current[edge.edge_id] = element;
              }}
            >
              {nodeLabel(edge.source, nodeById)} <span>{edge.relation}</span>{" "}
              {nodeLabel(edge.target, nodeById)}
              <SourceRefToggle
                expanded={expandedId === edge.edge_id}
                id={edge.edge_id}
                refs={edge.source_refs ?? []}
                transcript={transcript}
                visuals={visuals}
                onToggle={setExpandedId}
              />
            </div>
          ))
        )}
      </div>
    </div>
  );
}

type SourceRefToggleProps = {
  expanded: boolean;
  id: string;
  refs: SourceRef[];
  transcript: TranscriptSegment[];
  visuals: ImageCapture[];
  onToggle: (id: string | null) => void;
};

function SourceRefToggle({
  expanded,
  id,
  refs,
  transcript,
  visuals,
  onToggle,
}: SourceRefToggleProps) {
  if (refs.length === 0) {
    return <div className="graph-source-empty">暂无来源</div>;
  }

  return (
    <div className="graph-source-block">
      <button
        className="graph-source-toggle"
        type="button"
        onClick={() => onToggle(expanded ? null : id)}
      >
        来源 {refs.length}
      </button>
      {expanded ? (
        <div className="graph-source-list">
          {refs.map((ref) => (
            <div className="graph-source-ref" key={`${ref.type}-${ref.id}-${ref.ts ?? ""}`}>
              <span>{ref.type}</span>
              <code>{ref.id}</code>
              {typeof ref.ts === "number" ? <small>{ref.ts.toFixed(2)}s</small> : null}
              <p>{sourceRefText(ref, transcript, visuals)}</p>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function sourceRefText(
  ref: SourceRef,
  transcript: TranscriptSegment[],
  visuals: ImageCapture[],
): string {
  if (ref.text) {
    return ref.text;
  }
  if (ref.type === "segment") {
    return transcript.find((segment) => segment.segment_id === ref.id)?.text ?? "字幕来源未加载";
  }
  if (ref.type === "visual") {
    const visual = visuals.find((item) => item.image_id === ref.id);
    return (
      visual?.ocr_text
      || visual?.caption
      || visual?.visual_text?.join("；")
      || visual?.key_points?.join("；")
      || "视觉来源未加载"
    );
  }
  return "内部抽取事件";
}

type KnowledgeGraphCanvasProps = {
  focusedSource?: GlobalSearchSourceRef | null;
  graph: KnowledgeGraphView;
  nodeById: Map<string, KnowledgeNode>;
};

type GraphViewport = {
  scale: number;
  x: number;
  y: number;
};

type GraphDragState = {
  pointerId: number;
  lastClientX: number;
  lastClientY: number;
};

type GraphPoint = {
  x: number;
  y: number;
};

type GraphCluster = {
  nodeIds: string[];
  seedId: string;
};

const GRAPH_VIEWBOX_WIDTH = 820;
const GRAPH_VIEWBOX_HEIGHT = 500;
const GRAPH_DEFAULT_SCALE = 0.66;
const GRAPH_MIN_SCALE = 0.45;
const GRAPH_MAX_SCALE = 2.8;
const GRAPH_CENTER: GraphPoint = {
  x: GRAPH_VIEWBOX_WIDTH / 2,
  y: GRAPH_VIEWBOX_HEIGHT / 2,
};
const GRAPH_INITIAL_VIEWPORT: GraphViewport = {
  scale: GRAPH_DEFAULT_SCALE,
  x: (GRAPH_VIEWBOX_WIDTH * (1 - GRAPH_DEFAULT_SCALE)) / 2,
  y: (GRAPH_VIEWBOX_HEIGHT * (1 - GRAPH_DEFAULT_SCALE)) / 2,
};

function KnowledgeGraphCanvas({ focusedSource, graph, nodeById }: KnowledgeGraphCanvasProps) {
  // 轻量发散布局：高连接节点靠近中心，邻接节点向外散开，再做确定性碰撞分离。
  // 这样比单圈布局更接近 Obsidian 的双向链接图谱，也更容易看出关系簇。
  const layout = useMemo(
    () => createGraphLayout(graph.nodes, graph.edges),
    [graph.edges, graph.nodes],
  );
  const clusterByNode = useMemo(
    () => graphClusterByNode(graph.nodes, graph.edges),
    [graph.edges, graph.nodes],
  );
  const nodeWeights = useMemo(
    () => graphNodeWeights(graph.nodes, graph.edges),
    [graph.edges, graph.nodes],
  );
  const focusedId = focusedGraphId(focusedSource);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragStateRef = useRef<GraphDragState | null>(null);
  const [viewport, setViewport] = useState<GraphViewport>(GRAPH_INITIAL_VIEWPORT);
  const [isDragging, setIsDragging] = useState(false);
  const [isGraphFullscreen, setIsGraphFullscreen] = useState(false);

  useEffect(() => {
    function handleFullscreenChange() {
      setIsGraphFullscreen(document.fullscreenElement === canvasRef.current);
    }

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    handleFullscreenChange();
    return () => {
      document.removeEventListener("fullscreenchange", handleFullscreenChange);
    };
  }, []);

  const zoomByFactorAtPoint = useCallback((factor: number, point: { x: number; y: number }) => {
    setViewport((current) => {
      const scale = clamp(current.scale * factor, GRAPH_MIN_SCALE, GRAPH_MAX_SCALE);
      if (scale === current.scale) {
        return current;
      }

      const contentX = (point.x - current.x) / current.scale;
      const contentY = (point.y - current.y) / current.scale;
      return {
        scale,
        x: point.x - contentX * scale,
        y: point.y - contentY * scale,
      };
    });
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    const svg = svgRef.current;
    if (!canvas || !svg) {
      return;
    }
    const activeSvg = svg;

    function handleNativeWheel(event: WheelEvent) {
      event.preventDefault();
      event.stopPropagation();
      const point = svgPointFromClient(activeSvg, event.clientX, event.clientY);
      const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      zoomByFactorAtPoint(factor, point);
    }

    canvas.addEventListener("wheel", handleNativeWheel, { passive: false });
    return () => {
      canvas.removeEventListener("wheel", handleNativeWheel);
    };
  }, [zoomByFactorAtPoint]);

  const zoomFromCenter = useCallback(
    (factor: number) => {
      zoomByFactorAtPoint(factor, {
        x: GRAPH_VIEWBOX_WIDTH / 2,
        y: GRAPH_VIEWBOX_HEIGHT / 2,
      });
    },
    [zoomByFactorAtPoint],
  );

  function handlePointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    if (event.button !== 0) {
      return;
    }

    dragStateRef.current = {
      pointerId: event.pointerId,
      lastClientX: event.clientX,
      lastClientY: event.clientY,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setIsDragging(true);
  }

  function handlePointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.pointerId !== event.pointerId) {
      return;
    }

    const rect = event.currentTarget.getBoundingClientRect();
    const dx = ((event.clientX - dragState.lastClientX) * GRAPH_VIEWBOX_WIDTH) / rect.width;
    const dy = ((event.clientY - dragState.lastClientY) * GRAPH_VIEWBOX_HEIGHT) / rect.height;
    dragStateRef.current = {
      ...dragState,
      lastClientX: event.clientX,
      lastClientY: event.clientY,
    };
    setViewport((current) => ({
      ...current,
      x: current.x + dx,
      y: current.y + dy,
    }));
  }

  function handlePointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.pointerId !== event.pointerId) {
      return;
    }

    dragStateRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setIsDragging(false);
  }

  function resetViewport() {
    setViewport(GRAPH_INITIAL_VIEWPORT);
  }

  async function toggleGraphFullscreen() {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    try {
      if (document.fullscreenElement === canvas) {
        await document.exitFullscreen();
        return;
      }

      await canvas.requestFullscreen();
    } catch {
      // Fullscreen requests can be rejected by browser focus or permission rules.
    }
  }

  return (
    <div
      ref={canvasRef}
      className={`graph-canvas ${isGraphFullscreen ? "fullscreen" : ""}`}
      aria-label="知识图谱图形视图"
    >
      <div className="graph-toolbar" aria-label="图谱视图控制">
        <button
          aria-label="放大知识图谱"
          title="放大"
          type="button"
          onClick={() => zoomFromCenter(1.18)}
        >
          +
        </button>
        <button
          aria-label="缩小知识图谱"
          title="缩小"
          type="button"
          onClick={() => zoomFromCenter(1 / 1.18)}
        >
          -
        </button>
        <button
          aria-label="重置知识图谱视图"
          title="重置视图"
          type="button"
          onClick={resetViewport}
        >
          1:1
        </button>
        <button
          aria-label={isGraphFullscreen ? "退出知识图谱全屏" : "知识图谱全屏"}
          title={isGraphFullscreen ? "退出全屏" : "全屏"}
          type="button"
          onClick={() => void toggleGraphFullscreen()}
        >
          {isGraphFullscreen ? "退出" : "全屏"}
        </button>
      </div>
      <svg
        ref={svgRef}
        className={isDragging ? "dragging" : ""}
        viewBox={`0 0 ${GRAPH_VIEWBOX_WIDTH} ${GRAPH_VIEWBOX_HEIGHT}`}
        role="img"
        aria-labelledby="graph-svg-title"
        onPointerCancel={handlePointerUp}
        onPointerDown={handlePointerDown}
        onPointerLeave={handlePointerUp}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
      >
        <title id="graph-svg-title">课堂知识图谱</title>
        <g transform={`translate(${viewport.x} ${viewport.y}) scale(${viewport.scale})`}>
          {graph.edges.map((edge) => {
            const source = layout.get(edge.source);
            const target = layout.get(edge.target);
            const sourceNode = nodeById.get(edge.source);
            const targetNode = nodeById.get(edge.target);

            if (!source || !target) {
              return null;
            }

            const line = graphEdgeLine(
              source,
              target,
              sourceNode ? nodeRadius(sourceNode, nodeWeights.get(sourceNode.node_id)) : 6,
              targetNode ? nodeRadius(targetNode, nodeWeights.get(targetNode.node_id)) : 6,
              edge.edge_id,
            );

            return (
              <g
                className={graphEdgeClassName(edge, focusedId, clusterByNode)}
                key={edge.edge_id}
              >
                <title>
                  {nodeLabel(edge.source, nodeById)} {edge.relation}{" "}
                  {nodeLabel(edge.target, nodeById)}
                </title>
                <line x1={line.x1} x2={line.x2} y1={line.y1} y2={line.y2} />
              </g>
            );
          })}

          {graph.nodes.map((node) => {
            const point = layout.get(node.node_id);

            if (!point) {
              return null;
            }

            const radius = nodeRadius(node, nodeWeights.get(node.node_id));
            const labelLines = graphLabelLines(node.label);
            return (
              <g
                className={`graph-node ${focusedId === node.node_id ? "focused-source" : ""}`}
                key={node.node_id}
              >
                <circle cx={point.x} cy={point.y} r={radius} />
                <title>{node.label}</title>
                <text x={point.x} y={point.y + radius + 10}>
                  {labelLines.map((line, index) => (
                    <tspan
                      dy={index === 0 ? 0 : 11}
                      key={`${node.node_id}-label-${index}`}
                      x={point.x}
                    >
                      {line}
                    </tspan>
                  ))}
                </text>
              </g>
            );
          })}
        </g>
      </svg>

      <div className="graph-caption">
        {graph.nodes.length} 个节点，{graph.edges.length} 条关系
        {graph.edges.length > 0 ? `，最新关系：${latestEdgeText(graph.edges, nodeById)}` : ""}
        ，缩放 {Math.round(viewport.scale * 100)}%
      </div>
    </div>
  );
}

function focusedGraphId(focusedSource?: GlobalSearchSourceRef | null): string | null {
  if (!focusedSource) {
    return null;
  }
  if (focusedSource.type === "knowledge_node" || focusedSource.type === "knowledge_edge") {
    return focusedSource.id;
  }
  return null;
}

function createGraphLayout(
  nodes: KnowledgeNode[],
  edges: KnowledgeEdge[],
): Map<string, GraphPoint> {
  const layout = new Map<string, GraphPoint>();
  if (nodes.length === 0) {
    return layout;
  }

  const nodeById = new Map(nodes.map((node) => [node.node_id, node]));
  const { adjacency, incoming, outgoing } = buildGraphTopology(nodes, edges);
  const components = connectedComponents(nodes, adjacency);
  const componentCenters = componentCenterPoints(components.length);
  const clusterByNode = graphClusterByNode(nodes, edges);

  components.forEach((component, componentIndex) => {
    const center = componentCenters[componentIndex] ?? GRAPH_CENTER;
    const clusters = relationshipClusters(component, nodeById, adjacency, incoming, outgoing);
    const clusterCenters = componentClusterCenterPoints(center, clusters.length);

    clusters.forEach((cluster, clusterIndex) => {
      const clusterCenter = clusterCenters[clusterIndex] ?? center;
      const sortedNodes = [...cluster.nodeIds].sort((left, right) =>
        nodeSortScore(right, nodeById, adjacency, incoming, outgoing)
        - nodeSortScore(left, nodeById, adjacency, incoming, outgoing),
      );
      const seedId = cluster.seedId;
      layout.set(seedId, clusterCenter);

      const memberIds = sortedNodes.filter((nodeId) => nodeId !== seedId);
      memberIds.forEach((nodeId, index) => {
        const nodeAngle = stableAngle(nodeId);
        const ring = Math.floor(index / 7);
        const ringStart = ring * 7;
        const ringSize = Math.min(7 + ring * 4, Math.max(1, memberIds.length - ringStart));
        const ringIndex = index - ringStart;
        const angleJitter = Math.sin(nodeAngle * 2.7) * 0.18;
        const radiusJitter = (nodeAngle / (Math.PI * 2) - 0.5) * 24;
        const angle =
          stableAngle(seedId)
          + (Math.PI * 2 * ringIndex) / ringSize
          + ring * 0.22
          + angleJitter;
        const distance = 52 + ring * 54 + Math.min(28, memberIds.length * 2.4) + radiusJitter;
        layout.set(nodeId, {
          x: clusterCenter.x + Math.cos(angle) * distance,
          y: clusterCenter.y + Math.sin(angle) * distance * 0.76,
        });
      });
    });
  });

  relaxGraphLayout(layout, nodes, edges, clusterByNode);
  return layout;
}

function graphEdgeClassName(
  edge: KnowledgeEdge,
  focusedId: string | null,
  clusterByNode: Map<string, string>,
): string {
  const sourceCluster = clusterByNode.get(edge.source);
  const targetCluster = clusterByNode.get(edge.target);
  const relationshipClass =
    sourceCluster && sourceCluster === targetCluster ? "graph-edge-local" : "graph-edge-bridge";
  return `graph-edge ${relationshipClass} ${focusedId === edge.edge_id ? "focused-source" : ""}`;
}

function graphClusterByNode(
  nodes: KnowledgeNode[],
  edges: KnowledgeEdge[],
): Map<string, string> {
  const { adjacency, incoming, outgoing } = buildGraphTopology(nodes, edges);
  const nodeById = new Map(nodes.map((node) => [node.node_id, node]));
  const clusterByNode = new Map<string, string>();

  connectedComponents(nodes, adjacency).forEach((component, componentIndex) => {
    const clusters = relationshipClusters(component, nodeById, adjacency, incoming, outgoing);
    clusters.forEach((cluster, clusterIndex) => {
      const clusterId = `component-${componentIndex}-cluster-${clusterIndex}-${cluster.seedId}`;
      cluster.nodeIds.forEach((nodeId) => {
        clusterByNode.set(nodeId, clusterId);
      });
    });
  });

  return clusterByNode;
}

function graphNodeWeights(
  nodes: KnowledgeNode[],
  edges: KnowledgeEdge[],
): Map<string, number> {
  const weights = new Map<string, number>();
  const nodeIds = new Set(nodes.map((node) => node.node_id));
  nodes.forEach((node) => weights.set(node.node_id, 0));
  edges.forEach((edge) => {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target) || edge.source === edge.target) {
      return;
    }
    weights.set(edge.source, (weights.get(edge.source) ?? 0) + 1.1);
    weights.set(edge.target, (weights.get(edge.target) ?? 0) + 1);
  });

  const maxWeight = Math.max(1, ...weights.values());
  weights.forEach((weight, nodeId) => {
    weights.set(nodeId, weight / maxWeight);
  });
  return weights;
}

function nodeRadius(node: KnowledgeNode, graphWeight = 0): number {
  // importance 是 0-1 的重要度；graphWeight 来自连接度。核心节点略大，
  // 外围节点保持克制，整体接近 Obsidian 的小圆点网络视图。
  const importance = typeof node.importance === "number" ? node.importance : 0.5;
  return 5 + Math.round(importance * 2 + graphWeight * 6);
}

function graphEdgeLine(
  source: GraphPoint,
  target: GraphPoint,
  sourceRadius: number,
  targetRadius: number,
  seed: string,
) {
  const vector = safeVector(source, target, seed);
  const startPadding = sourceRadius + 3;
  const endPadding = targetRadius + 3;
  return {
    x1: source.x + vector.unitX * startPadding,
    y1: source.y + vector.unitY * startPadding,
    x2: target.x - vector.unitX * endPadding,
    y2: target.y - vector.unitY * endPadding,
  };
}

function buildGraphTopology(nodes: KnowledgeNode[], edges: KnowledgeEdge[]) {
  const nodeIds = new Set(nodes.map((node) => node.node_id));
  const adjacency = new Map<string, Set<string>>();
  const incoming = new Map<string, number>();
  const outgoing = new Map<string, number>();

  nodes.forEach((node) => {
    adjacency.set(node.node_id, new Set());
    incoming.set(node.node_id, 0);
    outgoing.set(node.node_id, 0);
  });

  edges.forEach((edge) => {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target) || edge.source === edge.target) {
      return;
    }
    adjacency.get(edge.source)?.add(edge.target);
    adjacency.get(edge.target)?.add(edge.source);
    outgoing.set(edge.source, (outgoing.get(edge.source) ?? 0) + 1);
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
  });

  return { adjacency, incoming, outgoing };
}

function connectedComponents(
  nodes: KnowledgeNode[],
  adjacency: Map<string, Set<string>>,
): string[][] {
  const remaining = new Set(nodes.map((node) => node.node_id));
  const components: string[][] = [];

  nodes.forEach((node) => {
    if (!remaining.has(node.node_id)) {
      return;
    }
    const component: string[] = [];
    const queue = [node.node_id];
    remaining.delete(node.node_id);

    while (queue.length > 0) {
      const current = queue.shift();
      if (!current) {
        continue;
      }
      component.push(current);
      [...(adjacency.get(current) ?? [])].sort().forEach((next) => {
        if (!remaining.has(next)) {
          return;
        }
        remaining.delete(next);
        queue.push(next);
      });
    }
    components.push(component);
  });

  return components.sort((left, right) => right.length - left.length);
}

function relationshipClusters(
  component: string[],
  nodeById: Map<string, KnowledgeNode>,
  adjacency: Map<string, Set<string>>,
  incoming: Map<string, number>,
  outgoing: Map<string, number>,
): GraphCluster[] {
  if (component.length <= 1) {
    return component.map((nodeId) => ({ nodeIds: [nodeId], seedId: nodeId }));
  }

  const sortedNodes = [...component].sort((left, right) => {
    const scoreDelta =
      nodeSortScore(right, nodeById, adjacency, incoming, outgoing)
      - nodeSortScore(left, nodeById, adjacency, incoming, outgoing);
    return scoreDelta || left.localeCompare(right);
  });
  const primarySeed = sortedNodes[0] ?? component[0] ?? "";
  if (!primarySeed) {
    return [];
  }
  const secondaryCandidates = sortedNodes.filter(
    (nodeId, index) => index === 0 || (adjacency.get(nodeId)?.size ?? 0) > 1,
  );
  const seedCount = Math.min(
    secondaryCandidates.length,
    relationshipClusterSeedCount(component, adjacency),
  );
  const seeds = secondaryCandidates.slice(0, Math.max(1, seedCount));
  const clustered = new Map<string, string[]>();
  seeds.forEach((seed) => clustered.set(seed, []));

  component.forEach((nodeId) => {
    const bestSeed = seeds
      .map((seed, seedIndex) => ({
        seed,
        score: clusterAffinity(nodeId, seed, seedIndex, primarySeed, adjacency),
      }))
      .sort((left, right) => right.score - left.score || left.seed.localeCompare(right.seed))[0]
      ?.seed ?? primarySeed;
    clustered.get(bestSeed)?.push(nodeId);
  });

  return seeds
    .map((seed) => ({
      seedId: seed,
      nodeIds: clustered.get(seed) ?? [seed],
    }))
    .filter((cluster) => cluster.nodeIds.length > 0)
    .sort((left, right) => {
      const sizeDelta = right.nodeIds.length - left.nodeIds.length;
      if (sizeDelta !== 0) {
        return sizeDelta;
      }
      return (
        nodeSortScore(right.seedId, nodeById, adjacency, incoming, outgoing)
        - nodeSortScore(left.seedId, nodeById, adjacency, incoming, outgoing)
      );
    });
}

function relationshipClusterSeedCount(
  component: string[],
  adjacency: Map<string, Set<string>>,
): number {
  const multiLinkNodes = component.filter((nodeId) => (adjacency.get(nodeId)?.size ?? 0) > 1);
  if (multiLinkNodes.length <= 2) {
    return 1;
  }
  return clamp(Math.round(Math.sqrt(multiLinkNodes.length)), 2, Math.min(7, multiLinkNodes.length));
}

function clusterAffinity(
  nodeId: string,
  seedId: string,
  seedIndex: number,
  primarySeed: string,
  adjacency: Map<string, Set<string>>,
): number {
  if (nodeId === seedId) {
    return Number.POSITIVE_INFINITY;
  }

  const nodeNeighbors = adjacency.get(nodeId) ?? new Set();
  const seedNeighbors = adjacency.get(seedId) ?? new Set();
  const isPrimarySeed = seedId === primarySeed && seedIndex === 0;
  const directWeight = isPrimarySeed ? 2.4 : 7.2;
  const commonNeighborWeight = isPrimarySeed ? 0.7 : 2.1;
  const directScore = nodeNeighbors.has(seedId) ? directWeight : 0;
  const commonNeighborScore =
    commonNeighborCount(nodeNeighbors, seedNeighbors) * commonNeighborWeight;
  const seedDegreeScore = Math.min(2.4, seedNeighbors.size * 0.16);
  const stableTieBreaker = stableAngle(`${nodeId}:${seedId}`) / (Math.PI * 2) / 100;
  return directScore + commonNeighborScore + seedDegreeScore + stableTieBreaker;
}

function commonNeighborCount(left: Set<string>, right: Set<string>): number {
  let count = 0;
  left.forEach((item) => {
    if (right.has(item)) {
      count += 1;
    }
  });
  return count;
}

function componentClusterCenterPoints(center: GraphPoint, clusterCount: number): GraphPoint[] {
  if (clusterCount <= 1) {
    return [center];
  }

  const centers = [center];
  const radiusX = Math.min(250, 130 + clusterCount * 18);
  const radiusY = Math.min(145, 78 + clusterCount * 13);
  for (let index = 0; index < clusterCount - 1; index += 1) {
    const angle = -Math.PI / 2 + (Math.PI * 2 * index) / (clusterCount - 1);
    centers.push({
      x: center.x + Math.cos(angle) * radiusX,
      y: center.y + Math.sin(angle) * radiusY,
    });
  }
  return centers;
}

function componentCenterPoints(componentCount: number): GraphPoint[] {
  if (componentCount <= 1) {
    return [GRAPH_CENTER];
  }

  if (componentCount === 2) {
    return [GRAPH_CENTER, { x: GRAPH_CENTER.x + 230, y: GRAPH_CENTER.y }];
  }

  const centers = [GRAPH_CENTER];
  const radiusX = 190;
  const radiusY = 100;
  for (let index = 0; index < componentCount - 1; index += 1) {
    const angle = -Math.PI / 2 + (Math.PI * 2 * index) / (componentCount - 1);
    centers.push({
      x: GRAPH_CENTER.x + Math.cos(angle) * radiusX,
      y: GRAPH_CENTER.y + Math.sin(angle) * radiusY,
    });
  }
  return centers;
}

function nodeSortScore(
  nodeId: string,
  nodeById: Map<string, KnowledgeNode>,
  adjacency: Map<string, Set<string>>,
  incoming: Map<string, number>,
  outgoing: Map<string, number>,
): number {
  const node = nodeById.get(nodeId);
  const importance = typeof node?.importance === "number" ? node.importance : 0.5;
  const degree = adjacency.get(nodeId)?.size ?? 0;
  const outDegree = outgoing.get(nodeId) ?? 0;
  const inDegree = incoming.get(nodeId) ?? 0;
  const levelPenalty = typeof node?.level === "number" ? Math.max(0, node.level) * 0.5 : 0;
  const rootBonus = outDegree > 0 && inDegree === 0 ? 3 : 0;
  return degree * 5 + outDegree * 2 + importance * 2 + rootBonus - levelPenalty;
}

function relaxGraphLayout(
  layout: Map<string, GraphPoint>,
  nodes: KnowledgeNode[],
  edges: KnowledgeEdge[],
  clusterByNode: Map<string, string>,
): void {
  const nodeById = new Map(nodes.map((node) => [node.node_id, node]));
  const filteredEdges = edges.filter((edge) => layout.has(edge.source) && layout.has(edge.target));
  const nodeIds = nodes.map((node) => node.node_id);

  for (let iteration = 0; iteration < 72; iteration += 1) {
    filteredEdges.forEach((edge) => {
      const source = layout.get(edge.source);
      const target = layout.get(edge.target);
      if (!source || !target) {
        return;
      }

      const vector = safeVector(source, target, edge.edge_id);
      const sameCluster = clusterByNode.get(edge.source) === clusterByNode.get(edge.target);
      const desiredDistance = sameCluster ? 74 : 148;
      const force = (vector.distance - desiredDistance) * (sameCluster ? 0.019 : 0.008);
      source.x += vector.unitX * force;
      source.y += vector.unitY * force;
      target.x -= vector.unitX * force;
      target.y -= vector.unitY * force;
    });

    for (let leftIndex = 0; leftIndex < nodeIds.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < nodeIds.length; rightIndex += 1) {
        const leftId = nodeIds[leftIndex];
        const rightId = nodeIds[rightIndex];
        const left = layout.get(leftId);
        const right = layout.get(rightId);
        if (!left || !right) {
          continue;
        }

        const vector = safeVector(right, left, `${leftId}_${rightId}`);
        const sameCluster = clusterByNode.get(leftId) === clusterByNode.get(rightId);
        const minDistance =
          nodeSpacing(nodeById.get(leftId), nodeById.get(rightId)) * (sameCluster ? 0.88 : 1.24);
        if (vector.distance >= minDistance) {
          if (sameCluster && vector.distance > minDistance * 1.55) {
            const pull = Math.min((vector.distance - minDistance * 1.55) * 0.008, 0.9);
            left.x -= vector.unitX * pull;
            left.y -= vector.unitY * pull;
            right.x += vector.unitX * pull;
            right.y += vector.unitY * pull;
          }
          continue;
        }

        const force = (minDistance - vector.distance) * (sameCluster ? 0.42 : 0.58);
        left.x += vector.unitX * force;
        left.y += vector.unitY * force;
        right.x -= vector.unitX * force;
        right.y -= vector.unitY * force;
      }
    }

    layout.forEach((point, nodeId) => {
      point.x += (GRAPH_CENTER.x - point.x) * 0.0012;
      point.y += (GRAPH_CENTER.y - point.y) * 0.0012;
      clampGraphPoint(point, nodeById.get(nodeId));
    });
  }
}

function nodeSpacing(left?: KnowledgeNode, right?: KnowledgeNode): number {
  const leftBox = graphLabelBox(left?.label ?? "");
  const rightBox = graphLabelBox(right?.label ?? "");
  return (
    58
    + Math.max(leftBox.width, rightBox.width)
    + Math.max(leftBox.height, rightBox.height) * 0.4
  );
}

function clampGraphPoint(point: GraphPoint, node?: KnowledgeNode): void {
  const labelBox = graphLabelBox(node?.label ?? "");
  const xPadding = Math.max(42, labelBox.width / 2 + 8);
  const bottomPadding = Math.max(46, labelBox.height + 34);
  point.x = clamp(point.x, xPadding, GRAPH_VIEWBOX_WIDTH - xPadding);
  point.y = clamp(point.y, 34, GRAPH_VIEWBOX_HEIGHT - bottomPadding);
}

function safeVector(from: GraphPoint, to: GraphPoint, seed: string) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const distance = Math.hypot(dx, dy);
  if (distance > 0.001) {
    return {
      distance,
      unitX: dx / distance,
      unitY: dy / distance,
    };
  }

  const angle = stableAngle(seed);
  return {
    distance: 0.001,
    unitX: Math.cos(angle),
    unitY: Math.sin(angle),
  };
}

function stableAngle(seed: string): number {
  let hash = 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) >>> 0;
  }
  return (hash / 0xffffffff) * Math.PI * 2;
}

function nodeLabel(nodeId: string, nodeById: Map<string, KnowledgeNode>): string {
  return nodeById.get(nodeId)?.label || nodeId;
}

function latestEdgeText(edges: KnowledgeEdge[], nodeById: Map<string, KnowledgeNode>): string {
  const latestEdge = edges[edges.length - 1];
  return `${nodeLabel(latestEdge.source, nodeById)} ${latestEdge.relation} ${nodeLabel(
    latestEdge.target,
    nodeById,
  )}`;
}

function graphLabelLines(label: string): string[] {
  const normalized = label.trim().replace(/\s+/g, " ");
  if (!normalized) {
    return [];
  }

  const maxUnits = 13;
  const tokens = graphLabelTokens(normalized);
  const lines: string[] = [];
  let currentLine = "";

  tokens.forEach((token) => {
    const candidate = joinGraphLabelToken(currentLine, token);
    if (!currentLine || graphTextUnits(candidate) <= maxUnits) {
      currentLine = candidate;
      return;
    }

    lines.push(...splitGraphLabelLine(currentLine, maxUnits));
    currentLine = token;
  });

  if (currentLine) {
    lines.push(...splitGraphLabelLine(currentLine, maxUnits));
  }

  return lines;
}

function graphLabelTokens(label: string): string[] {
  if (containsCjk(label) && !/\s/.test(label)) {
    return Array.from(label);
  }
  return label
    .replace(/([-/])/g, "$1 ")
    .split(/\s+/)
    .map((token) => token.trim())
    .filter(Boolean);
}

function joinGraphLabelToken(currentLine: string, token: string): string {
  if (!currentLine) {
    return token;
  }
  const currentChars = Array.from(currentLine);
  const tokenChars = Array.from(token);
  const lastCurrentChar = currentChars[currentChars.length - 1] ?? "";
  const firstTokenChar = tokenChars[0] ?? "";
  if (containsCjk(lastCurrentChar) || containsCjk(firstTokenChar)) {
    return `${currentLine}${token}`;
  }
  if (currentLine.endsWith("-") || currentLine.endsWith("/")) {
    return `${currentLine}${token}`;
  }
  return `${currentLine} ${token}`;
}

function splitGraphLabelLine(line: string, maxUnits: number): string[] {
  if (graphTextUnits(line) <= maxUnits) {
    return [line];
  }

  const chunks: string[] = [];
  let currentChunk = "";
  Array.from(line).forEach((char) => {
    const candidate = `${currentChunk}${char}`;
    if (currentChunk && graphTextUnits(candidate) > maxUnits) {
      chunks.push(currentChunk);
      currentChunk = char;
      return;
    }
    currentChunk = candidate;
  });
  if (currentChunk) {
    chunks.push(currentChunk);
  }
  return chunks;
}

function graphLabelBox(label: string) {
  const lines = graphLabelLines(label);
  const width = Math.max(0, ...lines.map((line) => graphTextUnits(line) * 5.6));
  return {
    width,
    height: lines.length * 11,
  };
}

function graphTextUnits(text: string): number {
  return Array.from(text).reduce((total, char) => {
    if (/\s/.test(char)) {
      return total + 0.45;
    }
    if (containsCjk(char)) {
      return total + 1.75;
    }
    if (/[A-Z]/.test(char)) {
      return total + 1.05;
    }
    if (/[-/_.]/.test(char)) {
      return total + 0.55;
    }
    return total + 0.9;
  }, 0);
}

function containsCjk(text: string): boolean {
  return /[\u3400-\u9fff]/.test(text);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function svgPointFromClient(
  svg: SVGSVGElement,
  clientX: number,
  clientY: number,
): { x: number; y: number } {
  const rect = svg.getBoundingClientRect();
  return {
    x: ((clientX - rect.left) * GRAPH_VIEWBOX_WIDTH) / rect.width,
    y: ((clientY - rect.top) * GRAPH_VIEWBOX_HEIGHT) / rect.height,
  };
}
