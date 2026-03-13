# 质量验证配置迁移文档

> 从旧配置迁移到统一质量验证模块

---

## 迁移对比

### 旧配置结构（重构前）

```jsonc
{
    "llm": {
        "min_calibrate_ratio": 0.8,

        "segmentation": {
            // 仅支持长度检查，无质量验证配置
        },

        "structured_calibration": {
            // ❌ 以下字段将被删除
            "quality_threshold": {
                "overall_score": 8.0,
                "minimum_single_score": 7.0
            },
            "enable_validation": false,
            "fallback_to_original": true,
            "validator_model": "deepseek-chat",
            "validator_reasoning_effort": null,
            "risk_validator_model": "gpt-4.1-mini",
            "risk_validator_reasoning_effort": null
        }
    }
}
```

### 新配置结构（重构后）

```jsonc
{
    "llm": {
        "min_calibrate_ratio": 0.7,  // ← 放宽到 0.7

        // ============ 新增：统一质量验证配置 ============
        "quality_validation": {
            "score_weights": {
                "accuracy": 0.40,
                "completeness": 0.30,
                "fluency": 0.20,
                "format": 0.10
            },
            "quality_threshold": {
                "overall_score": 8.0,
                "minimum_single_score": 7.0
            }
        },

        "segmentation": {
            // ============ 新增：质量验证配置 ============
            "quality_validation": {
                "enabled": true,  // ← 默认开启
                "pass_ratio": 0.7,
                "force_retry_ratio": 0.5,
                "fallback_strategy": "best_quality"
            }
        },

        "structured_calibration": {
            // ============ 修改：质量验证配置 ============
            "quality_validation": {
                "enabled": false,  // ← 默认关闭
                "fallback_strategy": "best_quality"
            }

            // ❌ 删除以下字段（自动处理）：
            // - quality_threshold
            // - enable_validation
            // - fallback_to_original
            // - validator_model
            // - validator_reasoning_effort
            // - risk_validator_model
            // - risk_validator_reasoning_effort
        }
    }
}
```

---

## 字段映射表

| 旧字段 | 新字段 | 迁移动作 |
|--------|--------|---------|
| `structured_calibration.quality_threshold` | `llm.quality_validation.quality_threshold` | **移动到统一配置** |
| `structured_calibration.enable_validation` | `structured_calibration.quality_validation.enabled` | **改名 + 默认改为 false** |
| `structured_calibration.fallback_to_original: true` | `structured_calibration.quality_validation.fallback_strategy: "formatted_original"` | **改为枚举值** |
| `structured_calibration.fallback_to_original: false` | `structured_calibration.quality_validation.fallback_strategy: "best_quality"` | **改为枚举值** |
| `structured_calibration.validator_model` | （删除） | **自动使用校对模型** |
| `structured_calibration.validator_reasoning_effort` | （删除） | **自动继承校对模型配置** |
| `structured_calibration.risk_validator_model` | （删除） | **自动处理风险场景** |
| `structured_calibration.risk_validator_reasoning_effort` | （删除） | **自动处理风险场景** |

---

## 迁移步骤

### 步骤 1：备份现有配置

```bash
cp config/config.jsonc config/config.jsonc.backup.$(date +%Y%m%d)
```

### 步骤 2：添加统一质量验证配置

在 `llm` 下新增：

```jsonc
"llm": {
    // ... 其他配置 ...

    "min_calibrate_ratio": 0.7,  // ← 从 0.8 改为 0.7

    // ============ 新增配置块 ============
    "quality_validation": {
        "score_weights": {
            "accuracy": 0.40,
            "completeness": 0.30,
            "fluency": 0.20,
            "format": 0.10
        },
        "quality_threshold": {
            "overall_score": 8.0,
            "minimum_single_score": 7.0
        }
    }
}
```

### 步骤 3：更新 segmentation 配置

在 `segmentation` 下新增：

```jsonc
"segmentation": {
    // ... 原有配置保持不变 ...

    // ============ 新增配置块 ============
    "quality_validation": {
        "enabled": true,
        "pass_ratio": 0.7,
        "force_retry_ratio": 0.5,
        "fallback_strategy": "best_quality"
    }
}
```

### 步骤 4：更新 structured_calibration 配置

**删除**以下字段：
```jsonc
"structured_calibration": {
    // ❌ 删除这些字段
    // "quality_threshold": { ... },
    // "enable_validation": false,
    // "fallback_to_original": true,
    // "validator_model": "deepseek-chat",
    // "validator_reasoning_effort": null,
    // "risk_validator_model": "gpt-4.1-mini",
    // "risk_validator_reasoning_effort": null
}
```

**新增**质量验证配置：
```jsonc
"structured_calibration": {
    // ... 原有配置保持不变 ...

    // ============ 新增配置块 ============
    "quality_validation": {
        "enabled": false,  // ← 默认关闭
        "fallback_strategy": "best_quality"
    }
}
```

### 步骤 5：验证配置

```bash
# 检查配置语法
python -m json.tool config/config.jsonc > /dev/null && echo "✅ 配置语法正确"

# 启动服务测试
uv run python main.py --start
```

---

## 常见场景迁移示例

### 场景 1：你之前开启了 structured_calibration 的质量验证

**旧配置**：
```jsonc
"structured_calibration": {
    "enable_validation": true,
    "fallback_to_original": false,
    "validator_model": "deepseek-chat",
    "quality_threshold": {
        "overall_score": 8.0,
        "minimum_single_score": 7.0
    }
}
```

**新配置**：
```jsonc
"llm": {
    "quality_validation": {
        "score_weights": {
            "accuracy": 0.40,
            "completeness": 0.30,
            "fluency": 0.20,
            "format": 0.10
        },
        "quality_threshold": {
            "overall_score": 8.0,  // ← 从旧配置移过来
            "minimum_single_score": 7.0
        }
    }
},

"structured_calibration": {
    "quality_validation": {
        "enabled": true,  // ← 对应 enable_validation: true
        "fallback_strategy": "best_quality"  // ← 对应 fallback_to_original: false
    }
}
```

**说明**：
- `validator_model` 删除，自动使用校对模型（`calibrate_model`）
- `quality_threshold` 移到统一配置

### 场景 2：你之前关闭了质量验证（默认场景）

**旧配置**：
```jsonc
"structured_calibration": {
    "enable_validation": false
}
```

**新配置**：
```jsonc
"structured_calibration": {
    "quality_validation": {
        "enabled": false  // ← 保持关闭
    }
}
```

### 场景 3：你使用了风险模型

**旧配置**：
```jsonc
"structured_calibration": {
    "enable_validation": true,
    "validator_model": "deepseek-chat",
    "risk_validator_model": "gpt-4.1-mini"
}
```

**新配置**：
```jsonc
"structured_calibration": {
    "quality_validation": {
        "enabled": true
    }
}
```

**说明**：
- 删除 `validator_model` 和 `risk_validator_model`
- 系统会自动使用：
  - 普通场景：`calibrate_model`
  - 风险场景：`risk_calibrate_model`

---

## 迁移检查清单

- [ ] 备份原配置文件
- [ ] 新增 `llm.quality_validation` 配置块
- [ ] 调整 `min_calibrate_ratio: 0.8 → 0.7`
- [ ] 新增 `segmentation.quality_validation` 配置块
- [ ] 删除 `structured_calibration` 中的废弃字段
- [ ] 新增 `structured_calibration.quality_validation` 配置块
- [ ] 检查配置语法
- [ ] 运行测试验证
- [ ] 查看日志确认质量验证开关生效

---

## 回滚步骤

如果迁移出现问题：

```bash
# 1. 停止服务
pkill -f "python main.py"

# 2. 恢复备份
cp config/config.jsonc.backup.YYYYMMDD config/config.jsonc

# 3. 切换到迁移前的代码版本
git checkout <迁移前的commit>

# 4. 重启服务
uv run python main.py --start
```

---

## 参考资料

- [统一质量验证设计文档](./unified_quality_validation_design.md)
- [配置文件完整示例](../../../config/config.example.jsonc)
