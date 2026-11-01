# 部署与维护

## 目录示例

| 位置 | 内容 |
| --- | --- |
| `/opt/wechat-autoreply` | 本仓库源码、虚拟环境和本地配置 |
| `/etc/wechat-autoreply.env` | API Key，格式见 `.env.example` |
| `/etc/wechat-autoreply/` | 本机账号数据库及媒体密钥，按代码所需格式自行配置 |
| `/var/lib/wechat-autoreply/` | 状态库、数据库快照缓存和表情素材 |

这些路径是部署约定，仓库不包含对应的私有文件。使用运行微信的专用用户访问它们，并给密钥文件设置最小读取权限；目录和文件应限制为该用户可访问。

## 启动顺序

1. 准备 X11、D-Bus、AT-SPI、微信客户端和专用用户。
2. 登录自己的测试微信，确认客户端会话可正常收发。
3. 安装依赖、复制并编辑配置示例，准备本机账号所需的私有密钥。
4. 在同一用户、同一图形会话内运行 `bridge.py --self-check`，再以前台方式验收。
5. 修改 `deploy/wechat-autoreply.service.example` 中的用户、路径、显示编号及 Xauthority。
6. 检查配置后安装服务，只保留一个执行器。

```bash
sudo cp deploy/wechat-autoreply.service.example /etc/systemd/system/wechat-autoreply.service
sudo systemctl daemon-reload
sudo systemctl enable --now wechat-autoreply.service
sudo systemctl status wechat-autoreply.service
```

示例不会自动创建或登录微信，也不安装虚拟桌面。D-Bus 与 AT-SPI 需要绑定实际登录会话，`DISPLAY` 和 `XAUTHORITY` 仅为示例。

## 额外能力

- 识图：`vision.enabled`、端点、模型及密钥路径必须对应真实图片输入能力。
- 表情：检查 `stickers` 开关、私有素材目录、视觉配置和 FFmpeg；保存与原生收藏分别验收。
- 语音：除可选 Python 包外，需要可用的 PulseAudio/虚拟麦克风与 FIFO 链路。仅安装 TTS 依赖不等于微信已能发送语音。
- 朋友圈：先保持 `dry_run: true`，测试定位后再启用具体动作；示例每日点赞与评论额度均为 20。
- 主动群聊：先验证 @ 与上下文，再开启 `group_interject_enabled` 等策略。

## 升级与回滚

升级前保存配置和源码，并在停止唯一执行器后备份状态数据库及其 WAL。部署完毕检查账号、进程和新消息回执。回滚源码时保留最新状态数据库、素材和动作回执；不要用旧库覆盖新库，以免再次发送已处理或结果未知的动作。

诊断日志只保留状态、耗时和脱敏事件标识。不要将服务日志、数据库、密钥或界面截图追加到公开 Issue。
