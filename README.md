# Kindle Desk

在 Linux 本机运行的 Kindle 管理界面。Python 3 标准库提供服务，无需 npm 构建；页面、数据库和字体均不依赖 CDN。EPUB 转换使用 calibre。

## 启动

在任意目录执行：

```sh
/home/rose/tools/kindle/start.sh
```

脚本会检查依赖并启动当前用户的后台服务，确认接口就绪后打印访问地址：<http://127.0.0.1:8765>。然后在这台 Linux 的浏览器打开即可使用。重复运行脚本会复用已运行的实例；关闭启动终端不会停止应用。

- 查看状态：`systemctl --user status kindle-desk`
- 运行期间重启：`systemctl --user restart kindle-desk`
- 停止：`systemctl --user stop kindle-desk`
- 查看日志：`journalctl --user -u kindle-desk -n 30 --no-pager`

没有设置开机自启。虚拟机重启后或手动停止服务后，重新运行 `start.sh` 即可。启动依赖 Python 3 和当前 Linux 用户的 systemd 会话。

默认设备路径为 `/run/media/rose/Kindle`。需要自定义设备路径或端口时，先停止后台服务，再直接运行 Python 服务：

```sh
systemctl --user stop kindle-desk
cd /home/rose/tools/kindle
python3 server.py --mount /实际/Kindle挂载点 --port 8766
```

此方式在当前终端前台运行，按 Ctrl+C 停止。服务仅监听 `127.0.0.1`；不能从 Windows 主机或其他电脑直接访问虚拟机内的这个地址。

calibre 隔离安装在本目录的 `runtime/calibre/`，程序会自动找到。代码、页面、测试、数据库和转换工具都在 `/home/rose/tools/kindle` 内，不依赖原来的 Codex 工作区。也可以通过 Python 服务的 `--converter /完整路径/ebook-convert` 参数指定其他转换工具。

## Docker 部署（Linux）

需要 Linux Docker Engine 和 Docker Compose。USB 仍由宿主机挂载，容器读取这个目录；无需把整个 USB 总线或宿主机系统服务交给容器。镜像内已安装 calibre 和中文字体，运行时无需联网下载转换工具。

首次准备，在项目目录执行：

```sh
mkdir -p data
cp .env.example .env
id -u
id -g
findmnt /run/media/rose/Kindle
```

编辑 `.env`：`KINDLE_MOUNT` 填实际挂载点，`LOCAL_UID`、`LOCAL_GID` 填上面两个 id 命令的结果。挂载点下应有 `documents`。默认数据目录为本项目的 `data/`，可继续使用已有摘录；更新时保留 `.env` 和 `data/`。

构建并启动：

```sh
docker compose build
# 若之前用 start.sh 启动，先停止，避免端口和数据目录被两个实例同时使用。
systemctl --user stop kindle-desk
docker compose up -d --wait
```

浏览器打开 <http://127.0.0.1:8765>。容器内部监听所有接口，但只向宿主机 `127.0.0.1:8765` 发布端口；原有本机 Host、Origin 和操作令牌校验保留。请保持映射两端端口一致为 8765。

常用命令：

```sh
docker compose ps
docker compose logs --tail=100 -f
docker compose down
# 更新代码后重建并重新创建容器，data/ 保留。
docker compose up -d --build --wait
```

拔出 Kindle 前执行 `docker compose down`，再从宿主机文件管理器安全弹出。重新插入并挂载后执行 `docker compose up -d --wait`。当前采用单个设备目录的 bind mount；USB 重新挂载后必须重新创建容器，单纯刷新页面或重启原容器不能代替这一步。未配置开机自启，避免设备未挂载时启动。容器里的“安全弹出”按钮禁用。

Kindle 和数据目录都必须事先存在；配置错误不会自动生成一个空的设备目录。容器以 `.env` 中的 UID/GID 运行，宿主机挂载必须允许该用户写入才能传书。容器不会修复或重新挂载只读设备。

迁移镜像到另一台同架构 Linux 机器：

```sh
mkdir -p dist
docker save kindle-desk:0.1.0 | gzip > dist/kindle-desk-0.1.0-linux-amd64.tar.gz
```

复制镜像包、`compose.yaml` 和 `.env.example` 到目标机器的同一目录。在目标机器执行：

```sh
gzip -dc kindle-desk-0.1.0-linux-amd64.tar.gz | docker load
mkdir -p data
cp .env.example .env
# 编辑挂载路径和 UID/GID，连接并挂载 Kindle 后启动；无需复制源代码。
docker compose up -d --no-build --wait
```

镜像不包含 `data/`、本机 `.env`、Git 历史或 `runtime/`。如果需要迁移已有摘录，先停止旧实例，再单独复制 `data/`。源码构建需要联网拉取基础镜像和 Debian 软件包；本次镜像为 Linux amd64，其他架构应在对应机器上重新构建。

挂载行为参考 [Docker bind mounts 文档](https://docs.docker.com/engine/storage/bind-mounts/)。本方式适用于已挂载 Kindle 的 Linux 主机或虚拟机；没有验证 Windows Docker Desktop 的 USB 转接。

## 当前能力

- 读取 USB 磁盘上的书籍、存储空间；重新连接后刷新，断开后保留最近一次书架。
- 解析中英文 `My Clippings.txt`，重复导入去重，按书名或内容搜索。
- 读取 `ksdk_annotation_v1.db` 的标注位置和可识别的笔记正文。每次读取先复制到电脑，设备数据库保持只读。
- 导出 Markdown ZIP，一本书一个文件，解压后可放入 Obsidian。
- 拖入 EPUB/PDF/AZW3/AZW/MOBI/TXT 后点击“传入 Kindle”。EPUB 本地转 AZW3，目标目录为 `documents/Kindle Desk`，拒绝覆盖同名文件。上限 200 MB。
- 下载设备上的原始书籍文件；这不等于去除 DRM 或保证其他阅读器可打开。
- 在用户点击“安全弹出”时调用 Linux udisksctl 卸载当前 Kindle 挂载点。

`data/desk.sqlite3` 保存本地索引、摘录和传输记录；`data/backups` 保存读取过的剪贴文件原件。删除本地数据目录会丢失离线记录。程序不会删除设备书籍或回写标注数据库。

## 已知边界

- 新版数据库中的高亮常常只包含位置。本版显示“正文尚未解析”，不会把内部位置冒充 Kindle 位置，也不会伪造高亮正文；完整正文解析仍需要真实标注样本和相应书籍格式支持。
- 数据库本地编辑操作码尚未通过实机样本确认；不认识的操作会提示，已导入记录保留。该版本是摘录归档，不会跟随设备端删除记录。
- 同一摘录在剪贴文件和数据库中可能重复；两者位置体系不同，本版保留来源，不自动跨来源合并。
- 电量、阅读百分比、当前打开的页面未实现。设备预览显示书架中的一本书，不代表正在阅读。
- “已复制”表示文件写入完成，不保证 Kindle 已完成索引或打开验证。
- 这次只验证 Linux。Windows 设备发现、启动器和打包尚未实现。

## 验证

```sh
python3 -m unittest -v test_server.py
```

测试使用临时目录模拟设备，不向实机写入测试书籍。
