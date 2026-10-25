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
- 配音（按质量优先自动接管，逐级降级）：
  1. **CosyVoice2** 本地大模型批量合成（质量第一梯队；需本机 `../cosyvoice` 环境，
     与军史号共享；`narrator.cosy.enable: true` 时接管，失败自动降级）；
  2. **edge-tts** 免费微软神经音色（被微软端点封堵时自动降级，恢复后自动回归）；
  3. **Piper** 本地神经音色（离线免费；模型 `../tts_models/en_US-ryan-medium.onnx`，
     需 `pip install piper-tts`；模型缺失时跳过该级）；
  4. **SAPI** 本地机械音：默认禁止，`allow_sapi_fallback: true` 才允许，
     且一旦使用，成片强制进入人工审核。
  全部神经后端不可用时直接停线等恢复——"产线可以断，烂片不能发"。
  实际使用的后端与音色逐段记录在任务 manifest 中。
- 不使用任何版权音乐 / 付费素材。

### 本机模型依赖（可选，缺失只降级不阻塞）

| 依赖 | 位置 | 缺失时行为 |
| --- | --- | --- |
| CosyVoice2 环境 | `../cosyvoice`（.venv + synth_cli.py + 模型） | 降级 edge → Piper |
| Piper 英文模型 | `../tts_models/en_US-ryan-medium.onnx` | 降级 SAPI（默认禁用→停线） |

## 使用

```bash
pip install -r requirements.txt
cp .env.example .env        # 填 ZHIPU_API_KEY / PEXELS_API_KEY
python autopost.py doctor   # 一键体检：启动检查 + TTS 链路真实探测（不耗额度）
python autopost.py          # 产出一期（自动选题、跳过已完成、断点续跑）
python autopost.py 3        # 指定主题序号
python autopost.py 3 --redo # 重置该选题重新制作
python autopost.py status   # 选题与任务状态总览
```

### 审核裁决（人工）

AI 审核给出 flagged（无论是否附带问题清单）或发现确定性问题时，任务停在 review 状态；
只有结构合法、verdict=pass 且问题列表为空才自动 ready。审核响应任何字段不合法
一律 fail-closed 等待重试，绝不放行：

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
| `another instance is running` | 锁由操作系统内核管理，**不需要也不要删除锁文件**；用任务管理器确认无 python 生产进程后直接重跑即可。锁文件头部记录持有人 pid/host 供诊断 |
| `no usable footage ... (ladder exhausted)` | 该段取材词太偏，改 `script.json` 里对应 `video_query` 后重跑 |
| `review channel unavailable` / `review response rejected` | 审核通道故障或响应不合法，文件已保留，下次运行自动重试（fail-closed，不会放行） |
| `QC failed` | 看 manifest 的 `qc_results`；坏片已拦截不会进 upload_info |
| 配音后端全部失败（质量闸门停线） | 跑 `python autopost.py doctor` 看哪一级可用；edge 封堵期间由 cosyvoice2/piper 自动接管 |

## 测试

```bash
python -m pytest -q        # 全部不依赖真实 API Key
```

## 相关项目

- [bili-history](https://github.com/zhaohongjun20-creator/bili-history) —— 同架构的B站军史频道产线（中文）
