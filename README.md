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

镜像包含 Python、calibre 和中文字体。运行它的 Linux 主机只需 Docker Engine、Docker Compose，以及通常自带的 Bash 和 `mountpoint`；不需要 Python、Node.js、calibre 或源代码，也不需要联网下载运行依赖。

**Docker 封装软件环境，设备和数据仍依赖宿主机。** Kindle 必须先被宿主机识别为磁盘并挂载，当前用户必须能读写 `documents/`。虚拟机还需把 USB 交给虚拟机。此部署验证的是本地 Linux Docker Engine；不是远程 Docker daemon，也没有验证 Windows/macOS Docker Desktop 的可移动磁盘共享、USB 转接和文件移动行为。只有 Docker 并不能保证设备可访问。镜像包的 CPU 架构也要与目标机器匹配。

### 离线包启动（推荐）

把 `kindle-desk-0.2.0-linux-amd64-bundle.tar.gz` 复制到另一台 Linux amd64 机器，解压并验证：

```sh
tar -xzf kindle-desk-0.2.0-linux-amd64-bundle.tar.gz
cd kindle-desk-0.2.0-linux-amd64
sha256sum -c SHA256SUMS
docker load -i kindle-desk-0.2.0-linux-amd64.tar.gz
# 先用宿主机文件管理器挂载 Kindle；替换成实际挂载路径。
./docker-start.sh /实际/Kindle挂载点
```

脚本校验挂载点、`documents/` 和写入权限，自动使用当前用户 UID/GID，并创建同目录的 `data/`。它固定使用随包的 Compose 文件，默认加载 `kindle-desk:0.2.0`，不会联网拉镜像或编译源码。无需创建 `.env`。查看地址：<http://127.0.0.1:8765>。

若本机版仍在运行，脚本会提示先执行 `systemctl --user stop kindle-desk`，避免两个实例占用同一端口或数据目录。它不会自动停止其他服务。Docker 运行时关闭终端不会停止应用；未配置开机自启。

若要用已有数据目录或其他镜像，显式传入环境变量（脚本不读取 `.env`）：

```sh
KINDLE_DATA=/绝对路径/原来的data KINDLE_IMAGE=kindle-desk:0.2.0 ./docker-start.sh /实际/Kindle挂载点
```

自定义 `KINDLE_DATA` 建议使用绝对路径；相对路径以脚本目录为基准。不要用 `sudo` 启动脚本；应让当前用户拥有 Docker 使用权限及 Kindle 写入权限。

### 停止、重新连接与备份

以下命令在部署目录执行，挂载路径替换成实际值：

```sh
KINDLE_MOUNT=/实际/Kindle挂载点 docker compose ps
KINDLE_MOUNT=/实际/Kindle挂载点 docker compose logs --tail=100 -f
# 拔线前先停止并移除容器，再从宿主机文件管理器安全弹出。
KINDLE_MOUNT=/实际/Kindle挂载点 docker compose down
# 重新插入、挂载后再启动，脚本会强制重建容器以使用新挂载。
./docker-start.sh /实际/Kindle挂载点
```

当前采用单个设备目录的 bind mount，USB 重新挂载后应重建容器；单纯刷新页面或重启原容器不能代替这一步。容器里的“安全弹出”按钮禁用。

`data/` 保存在宿主机，包含摘录数据库、离线书架、传输记录和未完成移动的恢复记录；重建容器不会删除它。升级或迁移时先停止旧实例，再备份或复制整个数据目录，启动新版时指向它。图书本体仍在 Kindle 中，备份 `data/` 不等于备份设备图书。离线包不包含个人数据、`.env` 或设备内容。

### 从源码构建

构建时需要联网拉取 Debian 基础镜像和软件包；构建完成后可离线运行。当前发布包为 Linux amd64，其他架构需要在对应机器上重新构建和验证。

```sh
docker build --build-arg VERSION=0.2.0 --build-arg VCS_REF="$(git rev-parse HEAD)" -t kindle-desk:0.2.0 .
./docker-start.sh /实际/Kindle挂载点
```

仅导出镜像可使用 `docker save kindle-desk:0.2.0 | gzip > kindle-desk-0.2.0-linux-amd64.tar.gz`。部署时还需要 `compose.yaml`、`docker-start.sh` 和此说明；不需要复制源代码。镜像的 OCI version/revision 标签记录版本和构建时传入的源码提交，完整离线包另附 `BUILD.txt` 和校验清单。

### 手动 Compose（可选）

如需自己管理配置，可复制 `.env.example` 为 `.env`，填写真实的挂载路径及 `id -u`、`id -g` 的值，先创建可写的 `data/`，再执行 `docker compose up -d --force-recreate --wait`。更新旧 `.env` 时应将 `KINDLE_IMAGE` 改为 `kindle-desk:0.2.0`。Compose 只运行已导入或已构建的镜像，不自动构建。

容器以普通用户运行，根文件系统只读，只挂载 Kindle 和数据目录，不授予 privileged 或 USB 总线访问。端口只发布到宿主机 `127.0.0.1:8765`；Host、Origin 和操作令牌校验保留，端口映射两端应保持为 8765。设备目录必须事先存在，错误路径不会被自动创建；只读设备和权限问题由宿主机处理。

参考 [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/) 和 [Docker Desktop USB 限制](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/general/#can-i-pass-through-a-usb-device-to-a-container)。

## 当前能力

- 读取 USB 磁盘上的书籍、存储空间；重新连接后刷新，断开后保留最近一次书架。
- 按 Kindle `documents/` 下的真实文件夹分类，显示已有分类及空目录；支持新建分类、子分类，传书时选择分类，以及移动单本书籍。
- 解析中英文 `My Clippings.txt`，重复导入去重，按书名或内容搜索。
- 读取 `ksdk_annotation_v1.db` 的标注位置和可识别的笔记正文。每次读取先复制到电脑，设备数据库保持只读。
- 导出 Markdown ZIP，一本书一个文件，解压后可放入 Obsidian。
- 拖入 EPUB/PDF/AZW3/AZW/MOBI/TXT 后选择“传入分类”，点击“传入 Kindle”。EPUB 本地转 AZW3；未选择分类时使用 `documents/Kindle Desk`，拒绝覆盖同名文件。上限 200 MB。
- 下载设备上的原始书籍文件；这不等于去除 DRM 或保证其他阅读器可打开。
- 在用户点击“安全弹出”时调用 Linux udisksctl 卸载当前 Kindle 挂载点。

`data/desk.sqlite3` 保存本地索引、摘录和传输记录；`data/backups` 保存读取过的剪贴文件原件。删除本地数据目录会丢失离线记录。程序不会删除设备书籍或回写标注数据库。

### 文件夹分类

在“设备书架”下拉框选择分类，只显示该文件夹直接包含的书籍；子文件夹会作为独立分类列出。“新建分类”可以选择上级目录，书籍旁的“移动”可选择目标分类。分类对应实际路径，例如 `文学/散文` 就是 Kindle 上的 `documents/文学/散文`，重新连接和离线缓存都保留这个结构。

移动时保持书籍文件名不变，并一起移动同名 `.sdr` 目录及可识别的 `.mbp`、`.mbp1`、`.mbs`、`.pdr`、`.apnx`、`.phl` 附件。目标位置出现同名文件或附件时拒绝移动；同名多格式书籍共用附件时也会拒绝，避免拆散关联。Linux 的不覆盖重命名操作用于防止并发覆盖。

一次移动涉及多个文件，不能保证断电或拔线时整体原子完成。程序会先在本机 `data/moves/` 写入位置记录，普通失败会尝试回退；移动完成或回退成功后删除记录。若设备断开导致回退失败，记录会保留，并暂停后续移动。此时先停止整理，核对记录中的 `from`、`to`，确认书籍与附件处于同一目录、文件完整后，再清除对应记录；不要覆盖冲突文件。记录本身不包含书籍副本。

整理完成后请安全弹出，让 Kindle 重新索引。文件夹分类用于本网页和磁盘文件整理，不会自动成为 Kindle 屏幕上的收藏集；原厂阅读界面的分类限制仍然适用。当前测试使用模拟设备，未对真实书籍的重索引、阅读进度或 KFX 附件关联作实机保证。

## 已知边界

- 新版数据库中的高亮常常只包含位置。本版显示“正文尚未解析”，不会把内部位置冒充 Kindle 位置，也不会伪造高亮正文；完整正文解析仍需要真实标注样本和相应书籍格式支持。
- 数据库本地编辑操作码尚未通过实机样本确认；不认识的操作会提示，已导入记录保留。该版本是摘录归档，不会跟随设备端删除记录。
- 同一摘录在剪贴文件和数据库中可能重复；两者位置体系不同，本版保留来源，不自动跨来源合并。
- 电量、阅读百分比、当前打开的页面未实现。设备预览显示书架中的一本书，不代表正在阅读。
- “已复制”表示文件写入完成，不保证 Kindle 已完成索引或打开验证。
- 这次只验证 Linux。Windows 设备发现、启动器和打包尚未实现。

## 验证

```sh
python3 -m unittest -v test_server.py test_docker_start.py
# 开发环境已构建镜像后，测试隔离容器、实际 EPUB 转换及重建后的数据保留。
python3 check_docker.py
```

测试使用临时目录模拟设备，不向实机写入测试书籍。容器测试使用随机本机端口和独立 Compose 项目，退出时清理测试容器，不停止现有服务。
