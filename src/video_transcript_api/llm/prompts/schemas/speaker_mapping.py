"""说话人映射 JSON Schema

用于说话人名称推断输出格式定义。

schema_version=2（证据裁决，card-vta-261015-speaker-evidence）：LLM 除给出
speaker_mapping/confidence 外，必须为每个说话人给出候选名列表，每个候选名
必须携带 evidence_type 与 evidence_quote。程序会核对每条 evidence_quote
是否逐字存在于本轮 prompt 输入文本中，核对失败的候选作废；显示名裁决完全
由核对通过的证据决定，confidence 仅作为诊断值记录进 meta，不参与采用判定。
"""

# 说话人映射产物的 schema 版本。v2 = 证据裁决（candidates + 引用核对）。
# 唯一定义处：cache_manager 写 envelope 时引用本常量，读侧接受 v1（旧
# confidence-gate 产物）与 v2，指纹不匹配时自然 miss。
SPEAKER_MAPPING_SCHEMA_VERSION = 2

# 角色域值。程序侧 fail-closed：域值之外（含 unknown）的角色一律按
# 「角色未定」处理，两人通路随之关闭。
# - host：主持人（提问/引导方）
# - guest：被访谈嘉宾
# - unknown：分不清时的诚实答案
SPEAKER_ROLE_TYPES = (
    "host",
    "guest",
    "unknown",
)

# 证据类型域值。程序侧 fail-closed：域值之外的 evidence_type 一律作废。
# - dialog_address：对话内他人称呼/自我介绍（单证据即可 applied）
# - title_credit：标题专场名（须另有 dialog_address 佐证才 applied）
# - author_host：作者署名（与 title_credit 同规则）
# - description_roster：描述参会名单（仅记录佐证，永不独立 applied——
#   名单里有名字 ≠ 该名字实际到场发言）
SPEAKER_EVIDENCE_TYPES = (
    "dialog_address",
    "title_credit",
    "author_host",
    "description_roster",
)

SPEAKER_MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker_mapping": {
            "type": "object",
            "description": "说话人标识到真实姓名的映射",
            "additionalProperties": {
                "type": "string"
            }
        },
        "confidence": {
            "type": "object",
            "description": "每个映射的置信度 (0-1)；仅作诊断记录，不参与采用判定",
            "additionalProperties": {
                "type": "number"
            }
        },
        "candidates": {
            "type": "object",
            "description": (
                "每个说话人的候选名列表；evidence_quote 必须逐字摘抄自输入文本，"
                "程序将逐字核对，核对失败的候选作废；evidence_type 为 "
                "dialog_address 时，evidence_quote 必须包含该候选名本身"
                "（如称呼句「大家好，我是柠姐」支持名字「柠姐」），"
                "未提及名字的发言段落不构成该名字的证据"
            ),
            "additionalProperties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "候选显示名",
                        },
                        "evidence_type": {
                            "type": "string",
                            "enum": list(SPEAKER_EVIDENCE_TYPES),
                            "description": "证据类型",
                        },
                        "evidence_quote": {
                            "type": "string",
                            "description": "逐字摘自输入文本的原文引用",
                        },
                    },
                    "required": ["name", "evidence_type", "evidence_quote"],
                    "additionalProperties": False,
                },
            },
        },
        "roles": {
            "type": "object",
            "description": (
                "每个说话人的角色判断：host=主持人（提问与引导方），"
                "guest=被访谈嘉宾，unknown=分不清时的诚实答案；"
                "判断依据只有对话内容，发布账号（作者）不一定是主持人"
            ),
            "additionalProperties": {
                "type": "string",
                "enum": list(SPEAKER_ROLE_TYPES),
            },
        },
        "reasoning": {
            "type": "string",
            "description": "推断依据说明"
        }
    },
    "required": ["speaker_mapping", "confidence", "candidates", "roles", "reasoning"],
    "additionalProperties": False
}
