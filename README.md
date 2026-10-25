# see-china · 带你看中国

面向海外观众的英文短视频产线：每期一个中国主题（山河/人文/工程/美食/历史），
自动生成英文脚本 + Pexels 实拍素材 + 神经配音字幕片，成片供手动上传
YouTube / Instagram 等海外平台。

## 产线

```
选题池(config.yaml 30题) → GLM 英文两步脚本(大纲→逐段, 带video_query)
  → Pexels 高清实拍素材搜索下载 → CosyVoice2/edge-tts 英文神经配音
  → 1920x1080/30fps 规范化剪辑(随机起点/循环补足) → 成片 output/<slug>/final.mp4
  → upload_info.txt(标题/描述/标签, 供手动上传)
```

## 素材与许可

- 视频：[Pexels License](https://www.pexels.com/license/)（免费商用，无需署名）
- 配音：本地 CosyVoice2（Apache-2.0）或 edge-tts
- 不使用任何版权音乐 / 付费素材

## 使用

```bash
cp .env.example .env   # 填 PEXELS_API_KEY / ZHIPU_API_KEY
python autopost.py     # 产出一期到 output/
```

## 相关项目

- [bili-history](https://github.com/zhaohongjun20-creator/bili-history) —— 同架构的B站军史频道产线（中文）
