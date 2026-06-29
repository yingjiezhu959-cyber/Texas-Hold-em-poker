# Texas Hold'em Poker

一个朋友娱乐局德州扑克网页应用，支持两人 / 三人对战、房间号加入、实时状态推送，以及明确标识的娱乐局功能。

## 本地运行

```bash
python server.py --host 127.0.0.1 --port 8010
```

打开：

```text
http://127.0.0.1:8010
```

## Render 部署

本仓库已包含 `render.yaml`，在 Render 创建 Blueprint 或 Web Service 时使用：

- Build Command: `pip install -r requirements.txt`
- Start Command: `python server.py --host 0.0.0.0 --port $PORT`

## 说明

页面会明确显示“娱乐局 / 非公平牌局”。本项目只用于朋友娱乐局和虚拟筹码，不用于真钱赌博。
