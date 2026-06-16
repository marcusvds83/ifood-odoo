---
name: gaokao-collect-student-info
description: >-
  高考志愿填报信息采集：收集考生省份、分数、选科等 API 必填项，以及兴趣、就业方向、
  意向城市/院校/专业类等倾向信息（供下游 API 选填参数使用），输出结构化 student.json。
  适用于高考志愿咨询开场、考生信息登记、志愿填报前的信息收集。
---

# 高考考生信息采集

本 Skill 是志愿推荐流水线的**第一步**，仅负责与用户对话并产出 `student.json`，不调用 API、不做推荐。

## 上下游

- **上游**：无
- **下游**：[gaokao-fetch-volunteers](../gaokao-fetch-volunteers/SKILL.md) 读取本 Skill 输出，将倾向映射为 API 选填参数

## 采集清单

### API 必填（写入 `student.json` 顶层）

| 字段 | 说明 | 示例 |
|------|------|------|
| `province` | 高考省份 | 山东 |
| `classify` | 文科/理科/物理/历史/综合 | 综合 |
| `score` | 高考成绩（整数） | 650 |
| `batch` | 填报批次 | 本科批 |
| `subjects` | 3+1+2 / 3+3 三科，逗号分隔；老高考填 `null` | 物理,化学,生物 |
| `gradeType` | 仅北京/上海/天津：本科/专科 | 本科 |
| `rank` | 位次，无则 `null` | 5000 |

### 辅助画像（Agent 分析与下游 API 倾向）

| 字段 | 说明 | 下游用途 |
|------|------|----------|
| `interests` | 兴趣爱好 | 推断 `preferred_major_classes` |
| `family_situation` | 家庭情况 | Agent 分析 |
| `career_direction` | 未来就业方向 | 推断 `preferred_major_classes` |
| `subject_scores` | 各科分数 | Agent 分析 |
| `preferred_cities` | 意向城市数组 | 推导 API `provinces` |
| `preferred_provinces` | 意向省份数组（可由城市推导） | API `provinces` |
| `preferred_universities` | 心仪院校数组 | API `universitys` |
| `preferred_tags` | 院校层次/属性，如 985、211 | API `tags` |
| `preferred_major_classes` | 专业类意向，如 计算机类 | API `majorClass` |
| `notes` | 排除性偏好等补充 | 后续推荐 Skill |

**倾向字段尽量在采集阶段结构化写入**，避免遗漏。映射规则见 [gaokao-fetch-volunteers/preference_mapping.md](../gaokao-fetch-volunteers/preference_mapping.md)。

## 工作流程

1. 用自然语言逐项询问，缺什么问什么；可分批提问。
2. 根据省份确认 `classify` / `subjects` / `gradeType` 是否必填，规则见 [reference.md](reference.md)。
3. 从对话中提取院校/城市/专业/层次偏好，写入 `preferred_*` 字段。
4. 信息齐全后保存 `output/student.json`，向用户复述并请确认。

## 输出格式

参考 [examples/student_template.json](examples/student_template.json)、[examples/student_shandong.json](examples/student_shandong.json)。

```json
{
  "province": "山东",
  "classify": "综合",
  "subjects": "物理,化学,生物",
  "score": 650,
  "batch": "本科批",
  "rank": null,
  "gradeType": null,
  "interests": "编程、人工智能",
  "career_direction": "互联网/硬科技",
  "preferred_cities": ["北京", "上海", "深圳"],
  "preferred_provinces": ["北京", "上海", "广东"],
  "preferred_universities": ["北京航空航天大学"],
  "preferred_tags": ["985", "211"],
  "preferred_major_classes": ["计算机类", "电子信息类"],
  "notes": "不接受偏远地区"
}
```

## 注意事项

- `batch` 下游会自动修正，此处可填用户口中的「本科批」。
- 排斥性偏好（如不要偏远）只写 `notes`，**不要**写入 `preferred_provinces`。
- 输出路径使用绝对路径交付用户。

## 附加资源

- [reference.md](reference.md) — 省份选科模式与 batch 对照
- [preference_mapping.md](../gaokao-fetch-volunteers/preference_mapping.md) — 倾向 → API 参数映射
