"""Centralized prompt builders used by LLM and local Qwen features.

All model-facing instructions live here so prompt wording, JSON schemas, and
grounding rules can be reviewed in one place. Callers should pass only the
runtime data needed to fill a prompt; validation of model output remains in the
feature modules that own each schema.
"""

from collections.abc import Mapping, Sequence


def summary_system_prompt() -> str:
    """System prompt for post-class summary generation."""
    return (
        "你是课堂学习助手。只能基于用户提供的课堂资料总结，不要编造。"
        "请输出 JSON object，字段为 summary_markdown 和 source_refs。"
        "source_refs 是数组，元素包含 type(segment 或 knowledge_node) 和 id。"
    )


def summary_user_prompt(classroom_brief: str) -> str:
    """User prompt for post-class summary generation."""
    return (
        "请使用课堂资料的主要授课语言生成一份简洁课堂总结，包含重点、知识脉络和复习建议。"
        "输出必须是 JSON，不要 Markdown code fence。\n\n"
        f"{classroom_brief}"
    )


def todo_system_prompt() -> str:
    """System prompt for structured todo extraction."""
    return (
        "你是课堂待办与复习计划助手。必须基于课堂资料工作，不要引入来源外内容。"
        "优先提取老师明确布置的任务、作业、预习、复习、提交或考试提醒。"
        "如果课堂没有明确待办，不要返回空数组；请基于课堂重点生成 3 到 5 个"
        "适合课后完成的学习待办，例如复习、整理笔记、完成概念自测、补充例题练习。"
        "生成型学习待办不得伪装成老师布置的作业，type 使用 generated_review、"
        "practice 或 review，confidence 不高于 0.65，due_time 返回 null。"
        "请输出 JSON object，字段为 todos。todos 是数组，每项包含 title、"
        "type、due_time、confidence、source_refs。source_refs 元素包含 "
        "type(segment 或 knowledge_node) 和 id，并引用支撑该学习待办的课堂来源。"
    )


def todo_user_prompt(classroom_brief: str) -> str:
    """User prompt for structured todo extraction."""
    return (
        "请从下面课堂资料中提取待办。如果没有明确待办，请生成 3 到 5 个"
        "基于课堂内容的课后学习待办。"
        "输出必须是 JSON，不要 Markdown code fence。\n\n"
        f"{classroom_brief}"
    )


def quiz_system_prompt() -> str:
    """System prompt for quiz generation."""
    return (
        "你是课堂自测题生成助手。只能基于课堂资料出题，不要引入课外内容。"
        "请输出 JSON object，字段为 quiz。quiz 是数组，每项包含 question、"
        "type、options、answer、explanation、source_refs。source_refs 元素"
        "包含 type(segment 或 knowledge_node) 和 id。"
    )


def quiz_user_prompt(classroom_brief: str) -> str:
    """User prompt for quiz generation."""
    return (
        "请使用课堂资料的主要授课语言生成 3 到 5 道自测题，优先覆盖关键概念。输出必须是 JSON，"
        "不要 Markdown code fence。\n\n"
        f"{classroom_brief}"
    )


def grounded_qa_system_prompt() -> str:
    """System prompt for grounded classroom QA."""
    return (
        "你是课堂答疑助手。必须优先依据课堂来源回答；可以使用你的通用"
        "知识补充解释，但必须明确区分课堂内容和补充解释。不要编造课堂"
        "中没有出现过的来源。请输出 JSON object，字段 answer。"
    )


def strict_qa_system_prompt() -> str:
    """System prompt for source-only classroom QA."""
    return (
        "你是课堂答疑助手。只能依据用户提供的课堂来源回答，禁止使用来源外"
        "常识、猜测或补全。若来源不足以回答问题，请直接说明“课堂资料中没有"
        "足够依据”。请输出 JSON object，字段 answer。"
    )


def strict_qa_user_prompt(
    *,
    student_prompt: str,
    retrieved_answer: str,
    source_refs: Sequence[Mapping[str, object]],
) -> str:
    """User prompt for source-only classroom QA."""
    refs = "\n".join(
        f"- {item.get('type')}:"
        f"{item.get('id')}; ts={item.get('ts')}; text={item.get('text')}"
        for item in source_refs
    )
    return (
        f"学生问题：{student_prompt}\n\n"
        f"检索摘要：{retrieved_answer}\n\n"
        "课堂来源：\n"
        f"{refs}\n\n"
        "请使用课堂来源的主要授课语言给出自然、连贯、适合学生复习的回答。"
        "只能使用上面课堂来源中的信息，不要增加来源外解释。"
        "如果来源不足，请说明不足并列出已经能确认的内容。"
    )


def grounded_qa_user_prompt(
    *,
    student_prompt: str,
    retrieved_answer: str,
    source_refs: Sequence[Mapping[str, object]],
) -> str:
    """User prompt for grounded classroom QA."""
    refs = "\n".join(
        f"- {item.get('type')}:"
        f"{item.get('id')}; ts={item.get('ts')}; text={item.get('text')}"
        for item in source_refs
    )
    return (
        f"学生问题：{student_prompt}\n\n"
        f"课堂检索回答：{retrieved_answer}\n\n"
        "课堂来源：\n"
        f"{refs}\n\n"
        "请使用课堂来源的主要授课语言回答，格式上明确区分课堂依据和补充解释。"
    )


def history_review_qa_system_prompt() -> str:
    """System prompt for cross-classroom review QA."""
    return (
        "你是课后复习答疑助手。只能基于提供的历史课堂命中来源回答。"
        "请把多节课中相互相关的内容整理成清晰复习回答，可以按课程或课堂组织。"
        "禁止加入来源外知识；如果来源不足，请说明不足。"
        "请输出 JSON object，字段 answer。"
    )


def history_review_qa_user_prompt(
    *,
    student_prompt: str,
    hits: Sequence[Mapping[str, object]],
) -> str:
    """User prompt for cross-classroom review QA."""
    hit_lines = "\n".join(
        "- "
        f"session={item.get('session_id')}; "
        f"course={item.get('course') or '未命名课程'}; "
        f"title={item.get('title')}; "
        f"source={item.get('type')}:{item.get('id')}; "
        f"ts={item.get('ts')}; "
        f"text={item.get('text')}"
        for item in hits
    )
    return (
        f"复习问题：{student_prompt}\n\n"
        "历史课堂命中来源：\n"
        f"{hit_lines or '- none'}\n\n"
        "请使用历史课堂命中来源的主要授课语言回答。要求：\n"
        "1. 先给直接答案。\n"
        "2. 再按课堂或课程列出依据。\n"
        "3. 如果多个来源重复，只合并表达，不要重复堆砌。"
    )


def llm_knowledge_extractor_system_prompt() -> str:
    """System prompt for backend LLM knowledge extraction."""
    return (
        "You are EDU-Mate's internal classroom knowledge extractor. "
        "Return only a JSON object matching this schema: "
        "{extraction_id:string optional, source_segment_ids:string[], "
        "source_visual_ids:string[], entities:[{entity_id?:string, "
        "name:string, type:string, description?:string}], "
        "relations:[{source:string,target:string,relation:string}], "
        "importance:number optional}. "
        "Use only the provided classroom transcript/OCR/caption sources. "
        "Do not invent source ids. Prefer concise Chinese entity names. "
        "Do not create generic or placeholder entities such as 课程, 课堂, 内容, "
        "知识点, 概念, 重点, 问题, 例子, 方法, ent_1, e1, or 概念名. "
        "An entity name must be a concrete course concept, event, person, formula, "
        "term, theory, method, or named topic. "
        "Relations must use snake_case labels such as defines, mentions, "
        "related_to, maps_to, belongs_to, part_of, derives_from."
    )


def llm_knowledge_extractor_user_prompt(
    *,
    session_id: str,
    transcript: Sequence[Mapping[str, object]],
    visuals: Sequence[Mapping[str, object]],
) -> str:
    """User prompt for backend LLM knowledge extraction."""
    lines = [f"session_id: {session_id}", "", "transcript:"]
    if transcript:
        for segment in transcript:
            lines.append(
                "- "
                f"id={segment.get('id')}; "
                f"ts={_float(segment.get('start_ts')):.2f}-{_float(segment.get('end_ts')):.2f}; "
                f"text={segment.get('text') or ''}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "visuals:"])
    if visuals:
        for visual in visuals:
            lines.append(
                "- "
                f"id={visual.get('id')}; "
                f"ts={_float(visual.get('capture_ts')):.2f}; "
                f"ocr={visual.get('ocr') or ''}; "
                f"caption={visual.get('caption') or ''}; "
                f"visual_text={visual.get('visual_text') or ''}; "
                f"key_points={visual.get('key_points') or ''}"
            )
    else:
        lines.append("- none")
    return "\n".join(lines)


def visual_analysis_system_prompt() -> str:
    """System prompt for multimodal classroom image analysis."""
    return (
        "You are EDU-Mate's multimodal classroom visual analyst. "
        "Analyze the provided classroom camera snapshot using only the image "
        "and the supplied classroom context. Do not invent text that is not "
        "visible or supported. Return only a JSON object with this schema: "
        "{caption:string, visual_text:string[], key_points:string[], "
        "entities:[{entity_id?:string,name:string,type:string,description?:string}], "
        "relations:[{source:string,target:string,relation:string}], importance?:number}. "
        "Use the main teaching language of the context/image for caption, "
        "key_points, entity names, and descriptions. visual_text should contain "
        "short visible text snippets or formulas from the image; if no readable "
        "text exists, return an empty array. Extract only concrete course "
        "concepts, formulas, diagrams, terms, named methods, events, or people. "
        "Do not create generic placeholders such as 课程, 课堂, 内容, 图片, 图像, "
        "知识点, 概念, 重点, 方法, ent_1, or e1. Relations must use snake_case "
        "labels such as contains, shows, defines, part_of, related_to, "
        "example_of, contrasts_with, derives_from."
    )


def visual_analysis_user_prompt(
    *,
    session_id: str,
    image_id: str,
    capture_ts: float,
    recent_transcript: Sequence[Mapping[str, object]],
    existing_nodes: Sequence[str],
    structured_notes_excerpt: str | None = None,
) -> str:
    """User prompt for multimodal classroom image analysis."""
    transcript_lines = [
        f"- id={segment.get('segment_id') or segment.get('id')}; "
        f"ts={_float(segment.get('start_ts')):.2f}-{_float(segment.get('end_ts')):.2f}; "
        f"text={segment.get('text') or ''}"
        for segment in recent_transcript
    ] or ["- none"]
    node_lines = [f"- {name}" for name in existing_nodes[:80]] or ["- none"]
    return "\n".join(
        [
            f"session_id: {session_id}",
            f"image_id: {image_id}",
            f"capture_ts: {capture_ts:.2f}",
            "",
            "recent_transcript_context:",
            *transcript_lines,
            "",
            "existing_knowledge_nodes:",
            *node_lines,
            "",
            "structured_notes_excerpt:",
            structured_notes_excerpt or "none",
            "",
            "Analyze the attached classroom image. Use recent_transcript_context "
            "only to disambiguate what is being taught; do not add unrelated facts. "
            "If the image is not educationally useful, return empty entities and "
            "relations with a concise caption explaining what is visible.",
        ]
    )


def markdown_knowledge_tree_system_prompt() -> str:
    """System prompt for cloud extraction from structured Markdown notes."""
    return (
        "You are EDU-Mate's cloud classroom knowledge-tree agent. "
        "Return only one JSON object. Build a lightweight knowledge tree "
        "from the provided structured Markdown notes and recent source subtitles. "
        "Use only provided content; do not add outside facts. "
        "Each streaming request includes full notes for context plus a recent "
        "subtitle window for this update; extract only new or changed graph "
        "items grounded in the recent subtitle window, while using the existing "
        "graph and full notes to deduplicate and preserve hierarchy. "
        "source_segment_ids must list only the few direct supporting subtitle ids, "
        "at most 5, selected from the recent subtitle window; never return the "
        "full subtitle list. "
        "Use the main teaching language of the provided notes/subtitles for "
        "entity names, descriptions, session_title, and course. "
        "Make hierarchy parent-to-child: "
        "course/topic contains subtopic/concept. Relations should use "
        "snake_case labels such as contains, defines, causes, example_of, "
        "contrasts_with, leads_to, related_to. "
        "Do not create generic or placeholder entities such as 课程, 课堂, 内容, "
        "知识点, 概念, 重点, 问题, 例子, 方法, ent_1, e1, or 概念名; "
        "only extract concrete course concepts, events, people, formulas, terms, "
        "theories, methods, or named topics. For final snapshots, infer "
        "session_title as a short classroom title and course as the broader "
        "course/subject name when directly supported by the notes; otherwise "
        "return null. JSON schema: "
        "{extraction_id?:string, source_segment_ids:string[], "
        "entities:[{entity_id?:string,name:string,type:string,description?:string}], "
        "relations:[{source:string,target:string,relation:string}], "
        "importance?:number, session_title?:string|null, course?:string|null}."
    )


def markdown_knowledge_tree_user_prompt(
    *,
    session_id: str,
    snapshot_id: str,
    sequence: int,
    update_status: str,
    existing_nodes: Sequence[str],
    existing_edges: Sequence[str],
    source_segments: Sequence[Mapping[str, object]],
    markdown: str,
    recent_source_segments: Sequence[Mapping[str, object]] | None = None,
) -> str:
    """User prompt for cloud extraction from structured Markdown notes."""
    focused_segments = (
        list(recent_source_segments)
        if recent_source_segments is not None
        else list(source_segments)
    )
    recent_lines = [
        f"- id={segment.get('segment_id')}; "
        f"ts={_float(segment.get('start_ts')):.2f}-{_float(segment.get('end_ts')):.2f}; "
        f"text={segment.get('text') or ''}"
        for segment in focused_segments
    ] or ["- none"]
    return "\n".join(
        [
            f"session_id: {session_id}",
            f"snapshot_id: {snapshot_id}",
            f"sequence: {sequence}",
            f"update_status: {update_status}",
            f"full_source_subtitle_count: {len(source_segments)}",
            f"recent_source_subtitle_count: {len(focused_segments)}",
            "",
            "existing_graph_nodes:",
            ", ".join(existing_nodes) or "none",
            "",
            "existing_graph_edges:",
            "; ".join(existing_edges) or "none",
            "",
            "recent_source_subtitles_for_this_update:",
            *recent_lines,
            "",
            "full_structured_markdown_notes_context:",
            markdown,
            "",
            "Task: return only graph additions or refinements directly supported by "
            "recent_source_subtitles_for_this_update. Use full_structured_markdown_notes_context "
            "only to understand course context and avoid duplicates.",
            "",
            "Return JSON only:",
        ]
    )


def transcript_polish_prompt(
    *,
    raw_text: str,
    previous_context: Sequence[str],
) -> str:
    """Prompt for local Qwen subtitle punctuation and phonetic correction."""
    context = "\n".join(f"- {item}" for item in previous_context[-3:]) or "- 无"
    return (
        "你是课堂实时字幕润色助手。请只处理本次 Whisper 原始转写，让字幕适合直接展示："
        "补充标点；根据发音、近音、同音、上下文和课堂术语修正语音识别错别字；"
        "把粘连文本拆成语意通顺的自然短句。\n"
        "纠错原则：如果 Whisper 词面不通顺，但按读音能明显对应到常见词、课程术语或上下文术语，"
        "应改成正确写法，例如“靠点”可改为“考点”，“苏晨克”可改为“速成课”。"
        "修正后的句子不要求逐字出现在原文中，但读音、语义和信息量必须与本次原始转写一致。\n"
        "严格限制：不得总结，不得扩写，不得补全被截断的句子，不得加入原文没有的信息。"
        "不确定的词保持原样，不要猜测；上一段字幕上下文只用于辨认术语，禁止复制到输出。"
        "每个分句都必须只表达本次原始转写中已经出现的内容。\n"
        "只输出一个 JSON object，不要 Markdown，不要解释。\n"
        "JSON schema: {\"sentences\": [\"一句自然字幕\", \"下一句自然字幕\"]}\n\n"
        "上一段字幕上下文:\n"
        f"{context}\n\n"
        "Whisper 原始转写:\n"
        f"{raw_text.strip()}\n\n"
        "JSON:"
    )


def transcript_polish_repair_prompt(raw_text: str) -> str:
    """Repair prompt for malformed local Qwen subtitle polish output."""
    return (
        "下面的文本本应是课堂字幕润色 JSON，但格式不合法。\n"
        "请只输出一个合法 JSON object，不要解释，不要 Markdown。\n"
        "必须且只能包含 sentences 字段，类型为字符串数组。\n\n"
        f"原始文本:\n{raw_text}\n\n"
        "合法 JSON:"
    )


def local_qwen_extraction_prompt(
    *,
    session_id: str,
    segments: Sequence[Mapping[str, object]],
) -> str:
    """Prompt for local Qwen knowledge extraction from transcript segments."""
    source_lines = "\n".join(
        f"- id={segment.get('segment_id')}; "
        f"ts={_float(segment.get('start_ts')):.2f}-{_float(segment.get('end_ts')):.2f}; "
        f"text={segment.get('text') or ''}"
        for segment in segments
    )
    allowed_ids = ", ".join(str(segment.get("segment_id")) for segment in segments)
    return (
        "你是 EDU-Mate 的本地课堂知识图谱抽取器。\n"
        "请只根据给定字幕抽取轻量知识图谱，不要补充字幕中没有出现的信息。\n"
        "只输出一个 JSON object，不要 Markdown，不要解释。\n"
        "JSON schema:\n"
        "{\n"
        '  "entities": [\n'
        '    {"entity_id": "node_optional", "name": "概念名", "type": "concept", '
        '"description": "一句话定义或课堂依据"}\n'
        "  ],\n"
        '  "relations": [\n'
        '    {"source": "起点概念名", "target": "终点概念名", "relation": "related_to"}\n'
        "  ],\n"
        '  "source_segment_ids": ["seg_id"],\n'
        '  "importance": 0.0\n'
        "}\n"
        "要求：\n"
        "- entity name 使用简洁中文课堂术语。\n"
        "- 不要把 课程、课堂、内容、知识点、概念、重点、问题、例子、方法、"
        "ent_1、e1、概念名 这类泛化词或占位符作为实体。\n"
        "- 实体必须是具体课程概念、事件、人物、公式、术语、理论、方法或有名称的主题。\n"
        "- relation 使用 snake_case 英文标签，例如 defines, mentions, related_to, "
        "belongs_to, part_of, causes, contrasts_with。\n"
        f"- source_segment_ids 只能从这些 ID 中选择：{allowed_ids}。\n"
        "- 没有明确知识点时返回空 entities 和空 relations。\n\n"
        f"session_id: {session_id}\n"
        "字幕:\n"
        f"{source_lines}\n\n"
        "JSON:"
    )


def local_qwen_extraction_repair_prompt(raw_text: str) -> str:
    """Repair prompt for malformed local Qwen graph extraction output."""
    return (
        "下面的文本本应是知识图谱抽取 JSON，但格式不合法。\n"
        "请只输出一个合法 JSON object，不要解释，不要 Markdown。\n"
        "必须包含 entities、relations、source_segment_ids、importance 字段。\n\n"
        f"原始文本:\n{raw_text}\n\n"
        "合法 JSON:"
    )


def qwen_markdown_notes_prompt(
    *,
    segments: Sequence[Mapping[str, object]],
    domain_terms: Sequence[str],
) -> str:
    """Prompt for local Qwen Markdown classroom notes generation."""
    transcript = "\n".join(
        f"- id={segment.get('id') or ''}; "
        f"[{_float(segment.get('start')):.2f}-{_float(segment.get('end')):.2f}] "
        f"{segment.get('text') or ''}"
        for segment in segments
    )
    language = qwen_notes_prompt_language(segments)
    if language == "en":
        return _qwen_markdown_notes_prompt_en(
            transcript=transcript,
            domain_terms=domain_terms,
        )

    terms = "、".join(domain_terms)
    return (
        "你是一个课堂笔记整理助手。"
        "系统会每隔一段时间把当前累计的 WhisperLive 字幕发给你，"
        "请把它整理成会持续更新的课堂笔记，用于记录课堂内容、重点、概念和老师强调的备考信息。"
        "请只根据给定字幕整理结构化课堂笔记，并生成结构化 Markdown 所需 JSON。\n"
        "输出语言必须使用授课内容的主要语言；如果课堂主要使用英文，就输出英文笔记；"
        "如果课堂主要使用中文，就输出中文笔记。\n"
        "严格限制：不得扩写，不得添加字幕中没有的信息，不得编造例子；"
        "即使你知道相关背景知识，也不能把字幕没有说出的内容写进笔记。"
        "如果内容不足，就保持简短。\n"
        "整理目标：像认真听课的学生记笔记一样，优先保留课堂主线、知识点、定义、"
        "因果关系、老师强调的重点和可复习的条目；不要写成宣传文案或总结报告。\n"
        "笔记必须是整理后的课堂笔记，不要机械复制字幕原句；可以把多个零散字幕"
        "概括成一条有意义的知识点、定义、对比或因果关系。"
        "例如不要把“有些用户是移动的/有些用户是固定的”分成碎片条目，"
        "应整理为“系统需要处理移动、固定和高速宽带等异构用户场景”。\n"
        "不要输出逐句润色字幕，不要改写整段 WhisperLive 字幕；只在 summary、sections、"
        "keywords 这些笔记字段中做必要整理。\n"
        "可选课程关键词如下；如果未提供关键词，就只能依据字幕上下文做通用纠错："
        f"{terms or '未提供'}。\n"
        "Few-shot 校准规则：\n"
        "- 当课程关键词与 Whisper 字幕存在明显同音、近音、漏字或错字关系，"
        "并且上下文支持时，可在笔记条目中使用正确课程关键词。例如关键词“线性代数”，"
        "字幕“线形代数”可在笔记中整理为“线性代数”。\n"
        "- 当关键词“薛定谔方程”与字幕“学定额方程”在发音和上下文上明显对应，"
        "可在笔记中整理为“薛定谔方程”。\n"
        "- 即使没有课程关键词，只要上下文和发音明显支持，也可以在笔记中修正常见词或课堂术语，"
        "例如“苏晨克”可整理为“速成课”，“靠点/烤点”可整理为“考点”。\n"
        "- 候选修正会改变原意时，保持原字幕含义，不要猜测。\n"
        "这些保守修正规则只应用到 summary、sections、keywords。\n"
        "每个 summary 条目和 section bullet 都必须能在原始字幕中找到直接依据，"
        "并且必须填写 source_segment_ids。source_segment_ids 只能使用上方字幕里的 id，"
        "表示这条笔记依据了哪些字幕片段；不要编造不存在的 id。\n"
        "可以把多条字幕合并成一条自然课堂笔记，也可以做术语化整理；"
        "但每个结论都必须能由 source_segment_ids 对应的字幕支持。\n"
        "只输出一个 JSON object，不要 Markdown，不要解释。\n"
        "JSON schema:\n"
        "{\n"
        '  "summary": [\n'
        '    {"text": "要点1", "source_segment_ids": ["seg_whisperlive_..."]}\n'
        "  ],\n"
        '  "sections": [\n'
        '    {"heading": "小节标题", "bullets": [\n'
        '      {"text": "条目", "source_segment_ids": ["seg_whisperlive_..."]}\n'
        "    ]}\n"
        "  ],\n"
        '  "keywords": ["关键词"]\n'
        "}\n\n"
        "WhisperLive 字幕:\n"
        f"{transcript}\n\n"
        "JSON:"
    )


def _qwen_markdown_notes_prompt_en(
    *,
    transcript: str,
    domain_terms: Sequence[str],
) -> str:
    """English prompt for English classroom notes.

    Keeping the whole instruction in English is intentional: the local Qwen
    model tended to answer Chinese when the instruction text was Chinese, even
    when the lecture transcript was English.
    """
    terms = ", ".join(domain_terms)
    return (
        "You are a classroom note-taking assistant for live lecture transcripts.\n"
        "TARGET_OUTPUT_LANGUAGE: English.\n"
        "All values in summary, sections.heading, sections.bullets.text, and "
        "keywords MUST be written in English. Do not translate the lecture into "
        "Chinese. Do not output Chinese text unless the exact Chinese term appears "
        "in the transcript or domain terms.\n"
        "The system periodically sends WhisperLive transcript segments. Organize "
        "them into structured classroom notes for a continuously updated Markdown "
        "document.\n"
        "Use only the provided transcript. Do not add facts, examples, definitions, "
        "or background knowledge that are not supported by the transcript. Keep the "
        "notes concise when the transcript is short.\n"
        "Goal: write useful student notes that preserve the lecture thread, key "
        "concepts, definitions, relationships, emphasized points, and reviewable "
        "items. Do not polish line-by-line subtitles and do not rewrite the whole "
        "transcript.\n"
        "Prefer concise conceptual notes over transcript copies. Avoid bare "
        "subtitle fragments such as \"Some users are mobile\" as standalone notes; "
        "merge related fragments into a meaningful point, for example: \"Streaming "
        "systems must handle heterogeneous users, including mobile, fixed, and "
        "high-speed broadband users.\"\n"
        "You may merge multiple transcript segments into one natural note and may "
        "use standard terminology, but every note item must be supported by its "
        "source_segment_ids.\n"
        "Each summary item and each section bullet MUST include source_segment_ids. "
        "source_segment_ids may only contain ids from the transcript below. Never "
        "invent ids.\n"
        "Optional domain terms: "
        f"{terms or 'none'}.\n"
        "Conservative correction rule: only fix obvious speech-recognition spelling "
        "or terminology errors when the transcript context clearly supports the "
        "correction. If a correction would change meaning, keep the transcript "
        "meaning.\n"
        "Return exactly one JSON object. Do not return Markdown. Do not explain.\n"
        "JSON schema:\n"
        "{\n"
        '  "summary": [\n'
        '    {"text": "Key point in English", "source_segment_ids": ["seg_whisperlive_..."]}\n'
        "  ],\n"
        '  "sections": [\n'
        '    {"heading": "Section heading in English", "bullets": [\n'
        '      {"text": "Bullet in English", "source_segment_ids": ["seg_whisperlive_..."]}\n'
        "    ]}\n"
        "  ],\n"
        '  "keywords": ["English keyword"]\n'
        "}\n\n"
        "WhisperLive transcript segments:\n"
        f"{transcript}\n\n"
        "JSON:"
    )


def qwen_markdown_notes_repair_prompt(raw_text: str) -> str:
    """Repair prompt for malformed local Qwen Markdown-notes JSON output."""
    return (
        "下面文本本应是课堂笔记 JSON，但格式不合法。"
        "请只输出合法 JSON object，不要解释，不要 Markdown。"
        "必须包含 summary、sections、keywords 字段。"
        "summary 条目和 sections.bullets 条目应使用 {text, source_segment_ids} 结构；"
        "如果原文没有 source_segment_ids，保持空数组。\n\n"
        f"原始文本:\n{raw_text}\n\n"
        "合法 JSON:"
    )


def qwen_markdown_notes_quality_retry_prompt(
    *,
    segments: Sequence[Mapping[str, object]],
    domain_terms: Sequence[str],
    previous_output: str,
    reason: str,
) -> str:
    """Retry prompt when Qwen returned JSON but not useful structured notes."""
    base_prompt = qwen_markdown_notes_prompt(
        segments=segments,
        domain_terms=domain_terms,
    )
    if qwen_notes_prompt_language(segments) == "en":
        return (
            base_prompt
            + "\n\n"
            "The previous output may have been JSON, but it did not satisfy the "
            "structured classroom notes requirements.\n"
            f"Failure reason: {reason}\n"
            "Regenerate the notes. summary must contain at least 2 English items; "
            "sections must contain at least 1 English section with at least 2 "
            "English bullets when the transcript has enough content. keywords may "
            "be present but cannot be the only content.\n"
            "Every summary item and bullet must include source_segment_ids from "
            "the transcript input. Use only transcript-supported facts. Do not "
            "invent. Do not output Chinese. Do not copy raw subtitle fragments; "
            "merge related fragments into useful student notes.\n\n"
            f"Previous output:\n{previous_output}\n\n"
            "Regenerated valid JSON:"
        )
    return (
        base_prompt
        + "\n\n"
        "上一次输出虽然可能是 JSON，但不满足课堂结构化笔记质量要求。\n"
        f"不合格原因：{reason}\n"
        "必须重新生成：summary 至少 2 条；sections 至少 1 个小节，且每个小节"
        "至少包含 2 条 bullet；keywords 可以保留但不能是唯一内容。\n"
        "每条 summary/bullet 必须带 source_segment_ids，且 id 必须来自字幕输入。"
        "仍然必须只依据字幕，不要编造。不要复制字幕碎片，应该合并成有复习价值的"
        "课堂笔记条目。\n\n"
        f"上一次输出:\n{previous_output}\n\n"
        "重新输出合法 JSON:"
    )


def qwen_notes_prompt_language(segments: Sequence[Mapping[str, object]]) -> str:
    """Infer the target note language from transcript text."""
    text = "\n".join(str(segment.get("text") or "") for segment in segments)
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    return "en" if latin > 0 and cjk == 0 else "zh"



def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
