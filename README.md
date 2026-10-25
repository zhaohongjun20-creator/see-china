# see-china · 带你看中国

面向海外观众的英文视频产线：每期一个中国主题（山河/人文/工程/美食/历史），
自动生成英文脚本 + Pexels 实拍素材 + 神经配音字幕片，成片供手动上传
YouTube / Instagram 等海外平台。

## 产线（阶段化 + 断点续跑）

```
precheck 启动检查(不耗额度) → 单实例锁 → 原子领取选题
  → script      GLM 英文两步脚本(大纲→逐段, 每段 video_query, 结构校验+修复重试)
  → narration   逐段配音(段级缓存: 文本/音色/后端未变即复用)
  → materials   Pexels 素材(查询改写阶梯 + 全局下载缓存 + 来源清单)
  → clips       逐段 1920x1080@30 规范化渲染(片段级缓存)
  → compose     xfade 转场 + 旁白 adelay 音画对齐 + BGM ducking + 字幕
  → qc          技术质检(可解码/规格/时长/静音/削波/黑帧/字幕/封面)
  → review      内容审核(确定性检查 + AI 事实审核; 异常不放行, high 级问题人工裁决)
  → package     final.mp4 + cover.jpg + final.srt + upload_info.txt
```

每期一个任务目录 `data/jobs/<job_id>/`，`manifest.json` 记录全部阶段状态、
输入输出、失败原因与耗时；任何阶段失败后**重跑即从失败点继续**，
已完成的讲稿/配音/素材/片段不会重复花钱重做。

## 选题状态机

```
pending → generating → review → ready → published
              │           │
              ▼           ▼ (AI 审核发现 high 级问题)
            failed <──────┘   (review 状态等待 CLI 人工裁决)
```

- `ready`（成片完成）与 `published`（人工上传成功）分开记录。
- 失败记录阶段/原因/重试次数，超过 `max_retries` 后不再自动领取，需 `redo`。
- 旧版数据库（dryrun/done/failed:x 状态）自动增量迁移，历史数据不丢。

## 素材与许可

- 视频：[Pexels License](https://www.pexels.com/license/)（免费商用，无需署名）；
  每段素材的 asset_id / 来源页 / license 记录在任务目录 `materials/index.json`。
- 配音：edge-tts（免费神经音色）或智谱 cogtts；音色全灭时默认停线不降级，
  `allow_sapi_fallback: true` 才允许本地 SAPI 兜底。
- 不使用任何版权音乐 / 付费素材。

## 使用

```bash
pip install -r requirements.txt
cp .env.example .env        # 填 ZHIPU_API_KEY / PEXELS_API_KEY
python autopost.py          # 产出一期（自动选题、跳过已完成、断点续跑）
python autopost.py 3        # 指定主题序号
python autopost.py 3 --redo # 重置该选题重新制作
python autopost.py status   # 选题与任务状态总览
```

### 审核裁决（人工）

AI 审核发现 high 级问题（事实错误 / 关键数据缺来源）时，任务停在 review 状态：

```bash
python autopost.py review-job <job_id>            # 查看问题清单
python autopost.py review-job <job_id> --approve  # 人工确认没问题 → ready
python autopost.py review-job <job_id> --reject   # 打回 → failed
```

### 人工上传后回写

```bash
python autopost.py mark-published 3
```

## 常见问题排查

| 现象 | 处理 |
| --- | --- |
| `precheck failed` | 按提示补 .env / config.yaml / BGM 曲库；生成不会消耗额度 |
| `another instance is running` | 已有产线进程；确认无进程后删除 `data/autopost.lock` |
| `no usable footage ... (ladder exhausted)` | 该段取材词太偏，改 `script.json` 里对应 `video_query` 后重跑 |
| `review channel unavailable` | 审核服务故障，文件已保留，下次运行自动重试（不会放行） |
| `QC failed` | 看 manifest 的 `qc_results`；坏片已拦截不会进 upload_info |
| edge-tts 被封 | 等恢复了重跑（断点续跑），或临时开 `allow_sapi_fallback` |

## 测试

```bash
python -m pytest -q        # 全部不依赖真实 API Key
```

## 相关项目

- [bili-history](https://github.com/zhaohongjun20-creator/bili-history) —— 同架构的B站军史频道产线（中文）
