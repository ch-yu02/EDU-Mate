import unittest

from backend.app import prompts


class PromptTemplatesTest(unittest.TestCase):
    def test_skill_prompts_keep_expected_json_fields(self) -> None:
        self.assertIn("summary_markdown", prompts.summary_system_prompt())
        self.assertIn("todos", prompts.todo_system_prompt())
        self.assertIn("quiz", prompts.quiz_system_prompt())

    def test_todo_prompt_generates_study_todos_when_no_explicit_assignment(self) -> None:
        system_prompt = prompts.todo_system_prompt()
        user_prompt = prompts.todo_user_prompt("课堂资料：今天讲采样定理。")

        self.assertIn("不要返回空数组", system_prompt)
        self.assertIn("3 到 5", system_prompt)
        self.assertIn("generated_review", system_prompt)
        self.assertIn("生成 3 到 5 个", user_prompt)

    def test_grounded_qa_prompt_includes_question_answer_and_sources(self) -> None:
        prompt = prompts.grounded_qa_user_prompt(
            student_prompt="采样定理为什么重要？",
            retrieved_answer="根据课堂资料：采样定理描述采样恢复条件。",
            source_refs=[
                {
                    "type": "segment",
                    "id": "seg_001",
                    "ts": 1.0,
                    "text": "采样定理描述采样恢复条件。",
                }
            ],
        )

        self.assertIn("采样定理为什么重要", prompt)
        self.assertIn("课堂检索回答", prompt)
        self.assertIn("seg_001", prompt)

    def test_local_qwen_extraction_prompt_preserves_allowed_source_ids(self) -> None:
        prompt = prompts.local_qwen_extraction_prompt(
            session_id="lec_prompt_test",
            segments=[
                {
                    "segment_id": "seg_local_whisper_0001",
                    "start_ts": 0.0,
                    "end_ts": 15.0,
                    "text": "傅里叶变换可以把信号转换到频域。",
                }
            ],
        )

        self.assertIn("seg_local_whisper_0001", prompt)
        self.assertIn("entities", prompt)
        self.assertIn("relations", prompt)
        self.assertIn("泛化词或占位符", prompt)

    def test_transcript_polish_prompt_allows_phonetic_correction_without_expansion(self) -> None:
        prompt = prompts.transcript_polish_prompt(
            raw_text="今天讲线形代数和鸡器学习",
            previous_context=["上一段提到机器学习。"],
        )

        self.assertIn("发音", prompt)
        self.assertIn("近音", prompt)
        self.assertIn("语意通顺", prompt)
        self.assertIn("线形代数", prompt)
        self.assertIn("应改成正确写法", prompt)
        self.assertIn("不得扩写", prompt)
        self.assertIn("不得加入原文没有的信息", prompt)

    def test_qwen_markdown_prompt_keeps_classroom_notes_constraints(self) -> None:
        prompt = prompts.qwen_markdown_notes_prompt(
            segments=[
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "线形代数里今天讲矩阵。",
                }
            ],
            domain_terms=["线性代数", "矩阵"],
        )

        self.assertIn("课堂笔记整理助手", prompt)
        self.assertIn("课堂内容、重点", prompt)
        self.assertIn("Few-shot", prompt)
        self.assertIn("即使没有课程关键词", prompt)
        self.assertIn("不要输出逐句润色字幕", prompt)
        self.assertIn("速成课", prompt)
        self.assertIn("输出语言必须使用授课内容的主要语言", prompt)
        self.assertNotIn("clean_transcript", prompt)
        self.assertIn("线性代数、矩阵", prompt)

    def test_qwen_markdown_prompt_uses_english_instructions_for_english_class(self) -> None:
        prompt = prompts.qwen_markdown_notes_prompt(
            segments=[
                {
                    "id": "seg_whisperlive_001",
                    "start": 0.0,
                    "end": 5.0,
                    "text": "DNS translates host names to IP addresses.",
                }
            ],
            domain_terms=["DNS", "IP address"],
        )

        self.assertIn("TARGET_OUTPUT_LANGUAGE: English", prompt)
        self.assertIn("Do not translate the lecture into Chinese", prompt)
        self.assertIn("Key point in English", prompt)
        self.assertIn("source_segment_ids", prompt)
        self.assertIn("seg_whisperlive_001", prompt)
        self.assertIn("DNS, IP address", prompt)
        self.assertNotIn("你是一个课堂笔记整理助手", prompt)
        self.assertNotIn("要点1", prompt)

    def test_qwen_markdown_retry_prompt_keeps_english_language_constraint(self) -> None:
        prompt = prompts.qwen_markdown_notes_quality_retry_prompt(
            segments=[
                {
                    "id": "seg_whisperlive_001",
                    "start": 0.0,
                    "end": 5.0,
                    "text": "DNS translates host names to IP addresses.",
                }
            ],
            domain_terms=[],
            previous_output='{"summary":["DNS 是域名系统"]}',
            reason="language mismatch",
        )

        self.assertIn("TARGET_OUTPUT_LANGUAGE: English", prompt)
        self.assertIn("Do not output Chinese", prompt)
        self.assertIn("Regenerated valid JSON", prompt)
        self.assertNotIn("重新输出合法 JSON", prompt)

    def test_markdown_knowledge_tree_prompt_includes_existing_graph_context(self) -> None:
        prompt = prompts.markdown_knowledge_tree_user_prompt(
            session_id="lec_notes",
            snapshot_id="snap_001",
            sequence=3,
            update_status="periodic",
            existing_nodes=["傅里叶变换"],
            existing_edges=["傅里叶变换 related_to 频域"],
            source_segments=[
                {
                    "segment_id": "seg_001",
                    "start_ts": 2.0,
                    "end_ts": 6.0,
                    "text": "傅里叶变换用于频域分析。",
                }
            ],
            markdown="# 课堂笔记\n- 傅里叶变换用于频域分析。",
        )

        self.assertIn("existing_graph_nodes", prompt)
        self.assertIn("傅里叶变换 related_to 频域", prompt)
        self.assertIn("recent_source_subtitles_for_this_update", prompt)
        self.assertIn("full_structured_markdown_notes_context", prompt)
        self.assertIn("seg_001", prompt)
        self.assertIn("Return JSON only", prompt)
        self.assertIn("generic or placeholder entities", prompts.markdown_knowledge_tree_system_prompt())
        self.assertIn("session_title", prompts.markdown_knowledge_tree_system_prompt())
        self.assertIn("main teaching language", prompts.markdown_knowledge_tree_system_prompt())


if __name__ == "__main__":
    unittest.main()
