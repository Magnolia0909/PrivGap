import json
import re
import coloredlogs, logging
from typing import List, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    import json_repair
except ImportError:
    json_repair = None
from data.data_process.data_structures import PrivacyItem
class LLMExtractor:
    def __init__(self, config, kb, llm_client):
        self.config = config
        self.kb = kb
        self.llm = llm_client
        self.logger = logging.getLogger(__name__)
        if self.llm is None or not hasattr(self.llm, "chat"):
            raise ValueError("[LLMExtractor] llm_client未提供或不支持 generate(prompt, **kwargs)。")
        self.batch_size = self.config.llm_batch_size
        self.max_workers = max(1, getattr(self.config, "llm_parallel_workers", 1))

    def extract(self, sentences: List[str], app_id: str) -> Tuple[List[PrivacyItem], int]:
        if not self.llm:
            return [], 0
        all_items: List[PrivacyItem] = []
        model_calls = 0
        batches = []
        for i in range(0, len(sentences), self.batch_size):
            batches.append((i, sentences[i:i + self.batch_size]))

        if self.max_workers > 1 and len(batches) > 1:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_start = {
                    executor.submit(self._process_batch, start, batch, app_id, sentences): start
                    for start, batch in batches
                }
                for future in as_completed(future_to_start):
                    start = future_to_start[future]
                    try:
                        items, calls = future.result()
                    except Exception as e:
                        self.logger.error(
                            f"[{app_id}] batch {start} 抽取失败，已跳过并保留其他 batch 结果: {e}"
                        )
                        continue
                    all_items.extend(items)
                    model_calls += calls
        else:
            for start, batch in batches:
                try:
                    items, calls = self._process_batch(start, batch, app_id, sentences)
                except Exception as e:
                    self.logger.error(
                        f"[{app_id}] batch {start} 抽取失败，已跳过并保留其他 batch 结果: {e}"
                    )
                    continue
                all_items.extend(items)
                model_calls += calls

        if not all_items:
            self.logger.warning(
                f"[{app_id}] LLMExtractor: 合法运行，但未抽取到任何隐私项（句子数={len(sentences)}）。"
            )
            return [], model_calls
        self.logger.info(f"[{app_id}] LLMExtractor 严格抽取完成：{len(all_items)} 条。")
        return all_items, model_calls

    def _process_batch(self, start: int, batch: List[str], app_id: str, all_sentences: List[str]) -> Tuple[List[PrivacyItem], int]:
        indexed_batch = self._build_context_batch(start, batch, all_sentences)
        model_calls = 0

        eligible_indices = None
        try:
            eligible_indices, classify_calls = self._classify_batch_resilient(indexed_batch, app_id, start)
            model_calls += classify_calls
        except Exception as e:
            self.logger.warning(f"[{app_id}] batch {start} 条款分类失败，回退为全部进入抽取: {e}")

        if eligible_indices is None:
            eligible_indices = {entry["idx"] for entry in indexed_batch}

        extraction_batch = [entry for entry in indexed_batch if entry["idx"] in eligible_indices]
        if not extraction_batch:
            self.logger.info(f"[{app_id}] batch {start} 条款筛选后无可抽取句子。")
            return [], model_calls

        items, extraction_calls = self._extract_batch_resilient(extraction_batch, app_id, start)
        model_calls += extraction_calls
        return items, model_calls

    def _classify_batch_resilient(self, indexed_batch: List[Dict[str, Any]], app_id: str, batch_start: int) -> Tuple[set, int]:
        try:
            return self._classify_batch(indexed_batch, app_id)
        except Exception as e:
            if not self._is_content_filter_error(e):
                raise
            if len(indexed_batch) <= 1:
                idx = indexed_batch[0]["idx"] if indexed_batch else "?"
                self.logger.warning(
                    f"[{app_id}] batch {batch_start} idx {idx} 条款分类被内容审核拒绝，跳过该句。"
                )
                return set(), 0
            mid = len(indexed_batch) // 2
            self.logger.warning(
                f"[{app_id}] batch {batch_start} 条款分类被内容审核拒绝，拆分为 "
                f"{len(indexed_batch[:mid])}+{len(indexed_batch[mid:])} 重试。"
            )
            left, left_calls = self._classify_batch_resilient(indexed_batch[:mid], app_id, batch_start)
            right, right_calls = self._classify_batch_resilient(indexed_batch[mid:], app_id, batch_start)
            return left | right, left_calls + right_calls

    def _extract_batch_resilient(self, indexed_batch: List[Dict[str, Any]], app_id: str, batch_start: int) -> Tuple[List[PrivacyItem], int]:
        try:
            return self._extract_batch_once(indexed_batch, app_id), 1
        except Exception as e:
            if not self._is_content_filter_error(e):
                raise
            if len(indexed_batch) <= 1:
                entry = indexed_batch[0] if indexed_batch else {}
                self.logger.warning(
                    f"[{app_id}] batch {batch_start} idx {entry.get('idx', '?')} 抽取被内容审核拒绝，跳过该句。"
                )
                return [], 0
            mid = len(indexed_batch) // 2
            self.logger.warning(
                f"[{app_id}] batch {batch_start} 抽取被内容审核拒绝，拆分为 "
                f"{len(indexed_batch[:mid])}+{len(indexed_batch[mid:])} 重试。"
            )
            left_items, left_calls = self._extract_batch_resilient(indexed_batch[:mid], app_id, batch_start)
            right_items, right_calls = self._extract_batch_resilient(indexed_batch[mid:], app_id, batch_start)
            return left_items + right_items, left_calls + right_calls

    def _extract_batch_once(self, indexed_batch: List[Dict[str, Any]], app_id: str) -> List[PrivacyItem]:
        raw = self._call_llm(indexed_batch, app_id)
        expected_indices = [entry["idx"] for entry in indexed_batch]
        triples_per_sentence = self._parse_and_validate_output(
            raw,
            expected_indices=expected_indices,
            app_id=app_id
        )
        triples_per_sentence = self._split_merged_triples(triples_per_sentence)
        triples_by_idx = {
            idx: triples
            for idx, triples in zip(expected_indices, triples_per_sentence)
        }

        items: List[PrivacyItem] = []
        for entry in indexed_batch:
            local_idx = entry["idx"]
            triples = triples_by_idx.get(local_idx, [])
            for t in triples:
                item = self._triple_to_item(t, entry["current"], sentence_id=entry["global_idx"])
                items.append(item)

        return items

    def _is_content_filter_error(self, error: Exception) -> bool:
        text = str(error).lower()
        markers = (
            "prohibited content",
            "内容审核",
            "敏感内容",
            "content policy",
            "content_filter",
            "safety",
            "模型调用被内容审核拒绝",
        )
        return any(marker in text for marker in markers)

    def _build_context_batch(self, start: int, batch: List[str], all_sentences: List[str]) -> List[Dict[str, Any]]:
        indexed_batch = []
        for local_idx, text in enumerate(batch):
            global_idx = start + local_idx
            indexed_batch.append({
                "idx": local_idx,
                "global_idx": global_idx,
                "prev": all_sentences[global_idx - 1] if global_idx > 0 else "",
                "current": text,
                "next": all_sentences[global_idx + 1] if global_idx + 1 < len(all_sentences) else "",
            })
        return indexed_batch

    def _classify_batch(self, indexed_batch: List[Dict[str, Any]], app_id: str) -> Tuple[set, int]:
        labels = (
            "collect_use_share, permission_action, definition, permission_notice, "
            "user_rights, security_retention, contact_meta, irrelevant"
        )
        prompt = (
            "你是中文小程序隐私政策条款筛选器。请判断每个 current 句子是否属于可抽取个人信息 data_type 的处理条款。\n\n"
            "可选标签：\n"
            "- collect_use_share: current 或相邻上下文表明正在收集、使用、共享、提供、处理个人信息/数据类型。\n"
            "- permission_action: current 描述为实现具体功能而调用或使用相机、相册、定位、麦克风、剪切板等权限。\n"
            "- definition: 定义、范围解释、术语说明，如“个人信息是指...”“包括但不限于...”。\n"
            "- permission_notice: 仅说明权限可开启/关闭/撤回、不会默认开启，没有具体处理场景。\n"
            "- user_rights: 访问、更正、删除、注销、撤回授权、投诉等用户权利。\n"
            "- security_retention: 安全保护、保存期限、匿名化、去标识化、未成年人保护、法律依据等。\n"
            "- contact_meta: 标题、目录、更新日期、生效日期、联系方式、政策说明。\n"
            "- irrelevant: 其他。\n\n"
            "判断规则：\n"
            "- prev/next 只用于判断 current 是否处在“我们收集以下信息/共享的信息包括”等条款块中。\n"
            "- 如果 current 是列表项，且 prev 或同段上下文是收集/使用/共享/提供信息的引导语，标 collect_use_share。\n"
            "- 如果 current 只是定义、举例、权利、安全、保存期限或联系方式，即使出现数据名，也不要标 collect_use_share。\n"
            "- 只输出 JSON 对象，键为 idx 字符串，值为上述标签之一；不要输出解释。\n\n"
            "输入：\n"
            + json.dumps([
                {
                    "idx": entry["idx"],
                    "prev": entry["prev"],
                    "current": entry["current"],
                    "next": entry["next"],
                }
                for entry in indexed_batch
            ], ensure_ascii=False, separators=(",", ":"))
            + f"\n\n标签集合：{labels}\n"
        )
        raw = self.llm.chat(
            prompt=prompt,
            app_id=app_id,
            task_type="policy_clause_classify"
        )
        parsed = self._parse_clause_classification(raw)
        eligible_labels = {"collect_use_share", "permission_action"}
        eligible_indices = {
            entry["idx"]
            for entry in indexed_batch
            if parsed.get(entry["idx"]) in eligible_labels
        }
        self.logger.info(
            f"[{app_id}] 条款筛选: eligible={len(eligible_indices)}/{len(indexed_batch)}"
        )
        return eligible_indices, 1

    def _parse_clause_classification(self, raw: str) -> Dict[int, str]:
        if getattr(self.config, "llm_backend", "") == "claude":
            raw = self._strip_markdown_json_fence(raw)
        try:
            parsed = json.loads(raw)
        except Exception:
            if json_repair is None:
                raise
            parsed = json_repair.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"条款分类输出不是 JSON object: {type(parsed)}")
        result = {}
        for key, value in parsed.items():
            try:
                idx = int(key)
            except Exception:
                continue
            if isinstance(value, str):
                result[idx] = value.strip()
            elif isinstance(value, dict) and isinstance(value.get("label"), str):
                result[idx] = value["label"].strip()
        return result

    def _strip_markdown_json_fence(self, raw: str) -> str:
        text = (raw or "").strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return text

    def _call_llm(self, indexed_batch: List[Dict[str, str]], app_id: str) -> str:
        guide = (
            "你是面向中文小程序隐私政策的 data_type 抽取器。输入来自已经通过条款筛选的短句或条款，"
            "每项包含 prev/current/next；prev 和 next 只作为上下文，不得从 prev/next 抽实体。\n\n"
            "【抽取目标】\n"
            "只抽取 current 句子中出现的 doccano 标注口径 data_type，并保留 current 句中出现的处理者/接收方。\n"
            "只输出可由同一句文本支持的两字段对象：\n"
            "- data_type: 被处理的个人信息/权限/数据类型，必须是原文中的最小完整片段。\n"
            "- recipients: 处理者、接收方或共享对象数组，优先用原文出现的主体；句中只有“我们/开发者/本平台/宝鸡婚恋网”等一方处理时填该主体；句中是第三方、关联公司、合作伙伴、SDK等接收时填对应原文片段。\n\n"
            "【输入输出格式】\n"
            "输入是JSON数组，每项包含 idx、prev、current、next、clause_type。输出必须是JSON对象，键为字符串化idx，值为 current 句的三元组数组。"
            "每个输入idx都必须出现；无可抽取关系时输出空数组[]。禁止输出解释、Markdown代码块、额外字段或<think>。\n\n"
            "【数据类型标注特点】\n"
            "- 保留原文粒度：如“微信昵称”“头像”“位置信息”“手机号”“相册（仅写入）权限”“选中的照片或视频信息”“剪切板”“设备信息”“日志信息”“订单信息”“身份证件号码”。\n"
            "- 并列数据要拆成多个三元组；括号内属于数据类型说明时保留，如“相册（仅写入）权限”。\n"
            "- 优先抽原文中的最小完整数据类型；如果原文只有“个人信息”“相关信息”“必要信息”“状态”“使用情况”等泛称且同句没有明确处理关系，不抽取。\n"
            "- 不要把具体数据类型泛化成“个人信息”，也不要把非数据片段如“服务”“功能”“活动”“下载”作为 data_type。\n"
            "- 若原句只是定义、范围解释、免责条件，或没有具体业务处理关系，输出[]。\n\n"
            "【处理语境要求】\n"
            "- current 可以是列表项；如果 prev/next 表明该列表属于收集、使用、共享、提供或处理信息的条款块，可以抽 current 中的 data_type。\n"
            "- 即使 prev/next 有数据类型，也只能抽 current 中出现的 data_type。\n"
            "- 如果只是定义、举例、用户权利、安全措施、保存期限、联系方式、投诉渠道、免责或否定共享，不抽取。\n"
            "- 不需要输出处理目的；目的只用于判断当前句是否属于可抽取的处理场景。\n\n"
            "【接收方标注特点】\n"
            "- 第一方处理：出现“开发者/我们/本平台/本公司/小程序名称/服务提供者”等，recipients填该原文主体；没有主体但语义承接明显为第一方时，可填“开发者”。\n"
            "- 对外提供：出现“第三方/关联公司/合作伙伴/授权合作伙伴/供应商/SDK/微信/支付宝”等，recipients填这些接收方原文片段。\n"
            "- 否定共享、尚未获得同意前不共享、仅说明第三方政策或用户自行向第三方提供，不抽取。\n\n"
            "【边界要求】\n"
            "- 不输出置信度、解释、理由或额外字段。\n"
            "- 低置信、需要跨句补全关键字段、或不确定是否属于标注口径时输出[]。\n"
        )

        known_terms = []
        try:
            if hasattr(self.kb, "knowledge"):
                known_terms = list(self.kb.knowledge.keys())
        except Exception as e:
            self.logger.warning(f"[LLMExtractor] 获取已知术语失败: {e}")
        
        memory_prompt = ""
        if known_terms:
            memory_prompt = "请参考以下已知隐私术语，以保持抽取一致性：\n"
            if known_terms:
                memory_prompt += f"【术语示例】{', '.join(known_terms)}\n"

        prompt = (
                memory_prompt
                + guide
                + "\n\n句子列表：\n"
                + json.dumps([
                    {
                        "idx": entry["idx"],
                        "prev": entry.get("prev", ""),
                        "current": entry.get("current", entry.get("text", "")),
                        "next": entry.get("next", ""),
                        "clause_type": entry.get("clause_type", "collect_use_share"),
                    }
                    for entry in indexed_batch
                ], ensure_ascii=False, separators=(",", ":"))

                + "\n\n【输出示例】\n"
                + '输入: [{"idx":0,"prev":"","current":"为了信息展示，开发者将在获取你的明示同意后，收集你的微信昵称、头像","next":""},'
                + '{"idx":1,"prev":"我们将收集以下信息","current":"地址、手机号","next":"用于给用户提供对应的信息资源"},'
                + '{"idx":2,"prev":"","current":"开发者读取你的剪切板，用于快速进行分享","next":""},'
                + '{"idx":3,"prev":"","current":"我们不会向第三方共享您的个人信息，但以下情况除外","next":""}]\n'
                + '输出: {"0":[{"data_type":"微信昵称","recipients":["开发者"]},'
                + '{"data_type":"头像","recipients":["开发者"]}],'
                + '"1":[{"data_type":"地址","recipients":["开发者"]},{"data_type":"手机号","recipients":["开发者"]}],'
                + '"2":[{"data_type":"剪切板","recipients":["开发者"]}],'
                + '"3":[]}\n\n'

                + "【更多判定示例】\n"
                + '✓ "为便于我们基于关联账号共同向您提供服务，您的账号信息可能会与我们的关联公司共享" → '
                + '[{"data_type":"账号信息","recipients":["关联公司"]}]\n'
                + '✓ "我们会向关联公司共享您必要的账号信息" → 若同句缺少明确处理场景则[]；不要跨句补全。\n'
                + '✓ "第三方和本平台一起为用户提供服务时，我们会向第三方提供您的手机号用于完成身份核验" → '
                + '[{"data_type":"手机号","recipients":["第三方"]}]\n'
                + '× "个人信息是指以电子或者其他方式记录的与已识别或者可识别的自然人有关的各种信息" → []\n'
                + '× "关于你的个人信息，你可以联系开发者行使查阅、复制、更正、删除等法定权利" → []\n'
                + '× "在未获得您的同意前，我们将不会向第三方共享您的个人信息" → []\n'
                + '✓ "我们将收集您的相关信息、必要信息、状态、频率，用于保障服务安全稳定运行" → '
                + '[{"data_type":"相关信息","recipients":["我们"]},{"data_type":"必要信息","recipients":["我们"]},{"data_type":"状态","recipients":["我们"]},{"data_type":"频率","recipients":["我们"]}]\n'
                + '× "与服务或功能相关的信息" → []；不要拆成“服务”或“功能相关的信息”。\n'
        )
        try:
            out = self.llm.chat(
                prompt=prompt,
                app_id=app_id,
                task_type="policy_extractor"
            )
        except Exception as e:
            raise RuntimeError(f"[LLMExtractor] LLM 调用失败: {e}")

        if not out or not isinstance(out, str):
            raise ValueError("[LLMExtractor] LLM 返回为空或类型非法。")
        
        return out

    def _parse_and_validate_output(self, raw: str, expected_indices: List[int], app_id: str) -> List[List[Dict[str, Any]]]:
        try:
            parsed = json.loads(raw)
        except Exception:
            if json_repair is None:
                raise ValueError(
                    f"[LLMExtractor] 标准 JSON 解析失败，且环境中未安装 json_repair。"
                    f"原文片段: {raw[:200]}..."
                )
            try:
                self.logger.warning(f"[{app_id}] 标准 JSON 解析失败，尝试使用 json_repair 修复...")
                parsed = json_repair.loads(raw)
                self.logger.info(f"[{app_id}] json_repair 修复成功。")
            except Exception as e:
                 raise ValueError(
                    f"[LLMExtractor] LLM 输出无法解析(json & json_repair)。原文片段: {raw[:200]}... 错误: {e}"
                )
        result_map: Dict[int, List[Dict[str, Any]]] = {}

        if isinstance(parsed, dict):
            for key, value in parsed.items():
                try:
                    idx = int(key)
                except Exception:
                    self.logger.warning(
                        f"[LLMExtractor] LLM 输出包含无法识别的键 '{key}'，已忽略。"
                    )
                    continue
                if isinstance(value, list):
                    result_map[idx] = value
                elif isinstance(value, dict):
                    result_map[idx] = [value]
                elif value is None:
                    result_map[idx] = []
                else:
                    self.logger.warning(
                        f"[LLMExtractor] 键 '{key}' 的值类型异常({type(value)}), 已视为缺失。"
                    )
                    result_map[idx] = []
        elif isinstance(parsed, list):
            normalized: List[List[Dict[str, Any]]] = []
            if parsed and all(isinstance(e, dict) for e in parsed):
                normalized = [parsed]
            else:
                normalized = parsed
            if len(normalized) != len(expected_indices):
                self.logger.warning(
                    f"[LLMExtractor] 输出长度 {len(normalized)} 与期望 {len(expected_indices)} 不符，"
                    "会按顺序补齐/截断。"
                )
            for idx, value in zip(expected_indices, normalized):
                if isinstance(value, list):
                    result_map[idx] = value
                elif value is None:
                    result_map[idx] = []
                else:
                    result_map[idx] = [value] if isinstance(value, dict) else []
        else:
            raise TypeError(
                f"[LLMExtractor] 解析结果类型不合法: {type(parsed)}"
            )

        triples_list: List[List[Dict[str, Any]]] = []
        for idx in expected_indices:
            triples_list.append(result_map.get(idx, []))
        for idx, elem in enumerate(triples_list):
            if not isinstance(elem, list):
                self.logger.warning(
                    f"[LLMExtractor] 第 {idx} 个元素不是数组(类型: {type(elem)})，已清空该句结果。"
                )
                triples_list[idx] = []
                continue

            if not elem:
                continue

            has_invalid = False
            for j, triple in enumerate(elem):
                try:
                    if not isinstance(triple, dict):
                        self.logger.warning(
                            f"[LLMExtractor] 第 {idx} 句的第 {j} 个三元组不是对象，已清空该句结果。"
                        )
                        has_invalid = True
                        break
                    for req in ("data_type", "recipients"):
                        if req not in triple:
                            self.logger.warning(
                                f"[LLMExtractor] 第 {idx} 句的第 {j} 个三元组缺少字段 {req}，已清空该句结果。"
                            )
                            has_invalid = True
                            break

                    if has_invalid:
                        break
                    if not isinstance(triple["data_type"], str) or not triple["data_type"].strip():
                        self.logger.warning(
                            f"[LLMExtractor] 第 {idx} 句的第 {j} 个三元组 data_type 非法，已清空该句结果。"
                        )
                        has_invalid = True
                        break
                    if not isinstance(triple["recipients"], list) or not all(
                        isinstance(r, str) for r in triple["recipients"]
                    ):
                        self.logger.warning(
                            f"[LLMExtractor] 第 {idx} 句的第 {j} 个三元组 recipients 非法，已清空该句结果。"
                        )
                        has_invalid = True
                        break

                except Exception as e:
                    self.logger.warning(
                        f"[LLMExtractor] 第 {idx} 句的第 {j} 个三元组验证时出现异常: {e}，已清空该句结果。"
                    )
                    has_invalid = True
                    break
            if has_invalid:
                triples_list[idx] = []

        non_empty = any(elem for elem in triples_list)
        if not non_empty:
            self.logger.warning(
                f"[LLMExtractor] 输出结构合法，但在 {len(expected_indices)} 个句子中未检测到任何隐私项。"
            )
        return triples_list

    def _triple_to_item(self, t: Dict[str, Any], sentence: str, sentence_id: int):
        data_type = t["data_type"].strip()
        purpose = ""
        recipients = [r.strip() for r in t["recipients"] if isinstance(r, str) and r.strip()]
        try:
            confidence = float(t.get("confidence", 1.0))
        except Exception:
            confidence = 1.0

        item = PrivacyItem(
            data_type=data_type,
            purpose=purpose,
            recipients=recipients,
            confidence=confidence,
            source="llm",
            evidence_text=sentence,
            sentence_id=sentence_id
        )
        return item
    
    def _split_merged_triples(self, triples_per_sentence: List[List[Dict[str, Any]]]) -> List[List[Dict[str, Any]]]:
        result = []
        separators = ["以及", "、", ",", "，", "和", "与", "及", "或"]
        for sentence_triples in triples_per_sentence:
            expanded_triples = []
            for triple in sentence_triples:
                data_type = triple["data_type"]
                purpose = triple.get("purpose", "")
                recipients = triple["recipients"]
                confidence = triple.get("confidence", 1.0)

                data_types = self._split_data_type(data_type, separators)
                if not data_types:
                    self.logger.info(f"过滤噪声 data_type: {data_type}")
                    continue
                if len(data_types) > 1:
                    self.logger.info(f"拆分'{data_type}'为{data_types}")
                    for dt in data_types:
                        expanded_triples.append({
                            "data_type": dt,
                            "purpose": purpose,
                            "recipients": recipients,
                            "confidence": confidence
                        })
                else:
                    normalized_triple = dict(triple)
                    normalized_triple["data_type"] = data_types[0]
                    expanded_triples.append(normalized_triple)
            result.append(expanded_triples)
        return result

    def _split_data_type(self, data_type: str, separators: List[str]) -> List[str]:
        has_parenthesis = bool(re.search(r'[（(].*?[）)]', data_type))
        if has_parenthesis:
            self.logger.debug(f"视括号为详细举例，不进行拆分: {data_type}")
            return [data_type.strip()]
        parts = [data_type]
        for sep in separators:
            new_parts = []
            for part in parts:
                if sep in part:
                    split_parts = [p.strip() for p in part.split(sep)]
                    new_parts.extend(split_parts)
                else:
                    new_parts.append(part)
            parts = new_parts
        result = [part.strip() for part in parts if part.strip()]
        if not result:
            return [data_type.strip()]
        result = [self._clean_split_data_type(part) for part in result]
        result = [part for part in result if self._is_valid_split_data_type(part)]
        if not result:
            cleaned = self._clean_split_data_type(data_type)
            return [cleaned] if self._is_valid_split_data_type(cleaned) else []
        if len(result) > 1:
            self.logger.info(f"拆分 '{data_type}' → {result}")
        return result

    def _clean_split_data_type(self, data_type: str) -> str:
        text = data_type.strip()
        text = re.sub(r"^(包含|包括|含有|涉及|基于上述信息形成的|与)", "", text).strip()
        text = re.sub(r"^(您的|你的|该)", "", text).strip()
        text = re.sub(r"^(或|及|和|与)", "", text).strip()
        return text

    def _is_valid_split_data_type(self, data_type: str) -> bool:
        text = data_type.strip()
        if not text:
            return False
        exact_noise = {
            "您", "你", "其", "该", "服务", "功能", "活动", "下载", "认证",
            "个人名下", "同类技术",
            "功能相关的信息", "服务或功能相关的信息",
        }
        if text in exact_noise:
            return False
        if len(text) <= 1:
            return False
        if re.fullmatch(r"(信息|数据|资料)", text):
            return False
        return True
