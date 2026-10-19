# Kindle Desk

通过 USB 连接 Kindle，在本地网页管理图书、文件夹和阅读摘录。无需账号，页面和数据都保存在本机，运行时无需联网。

当前版本 **0.2.0**。主要使用环境是 Linux 主机或 Linux 虚拟机，现有设备为 Kindle 第 11 代（2022），固件 5.19.2.0.1。发布镜像为 **Linux amd64**；其他设备、架构和操作系统没有完整验证。

| 可以做什么 | 当前行为 |
| --- | --- |
| 查看书架 | 读取设备图书、所在文件夹和存储空间；断开后保留最近一次书架 |
| 文件夹分类 | 新建分类、子分类，按目录筛选，将单本书籍和识别到的关联附件一起移动 |
| 传入图书 | EPUB 在本地转为 AZW3；PDF、AZW3、AZW、MOBI、TXT 直接复制 |
| 归档摘录 | 导入中英文 `My Clippings.txt`，读取部分设备标注数据库内容，搜索和筛选 |
| 导出与备份 | 摘录导出为 Markdown ZIP，一本书一个文件；下载单本原始书籍文件 |

**分类对应 Kindle 磁盘上的真实文件夹，不会自动成为 Kindle 屏幕上的收藏集。** 本应用不破解 DRM，也不修改 Kindle 固件。

## 1. 选择运行方式

| 方式 | 适合场景 | 主机需要准备 |
| --- | --- | --- |
| Docker 离线包（推荐） | 换电脑部署、避免安装应用依赖 | 本地 Linux Docker Engine、Docker Compose、Bash、`mountpoint` |
| 从源码构建 Docker 镜像 | 修改代码后部署 | 上述环境、源码，以及构建时的网络访问 |
| Linux 本机运行 | 本机开发、使用网页中的安全弹出 | Python 3；后台启动另需 systemd 用户会话；EPUB 转换另需 calibre |

Docker 镜像已经包含 Python、calibre 和中文字体，目标主机无需安装这些软件，也无需 Node.js、npm 或应用源码。但 USB 和文件权限仍由主机管理：

- Kindle 必须先被识别为磁盘并挂载，挂载点下有 `documents/`，当前用户能读写它。本版没有 MTP 连接适配。
- 使用虚拟机时，先将 USB 设备连接到该虚拟机，再在虚拟机里挂载。
- Docker 服务必须在设备所在的 Linux 主机上运行。这里不支持通过远程 Docker daemon 读取客户端本地 USB。
- Windows/macOS Docker Desktop 的设备共享和文件移动行为尚未验证，不能仅凭安装了 Docker 就认定可用。

默认地址是 <http://127.0.0.1:8765>，应在**运行服务的那台系统**的浏览器里打开。Linux 虚拟机中的 `127.0.0.1` 不等于 Windows 主机中的 `127.0.0.1`。

## 2. Docker 快速开始

### 准备设备

用系统文件管理器挂载 Kindle，并确认路径。例如本机使用 `/run/media/rose/Kindle`，其他机器以实际路径为准。

```sh
docker info
docker compose version
findmnt --mountpoint "/实际/Kindle挂载点"
ls "/实际/Kindle挂载点/documents"
```

后文的 `/实际/Kindle挂载点` 都需要替换。应传入 **Kindle 根挂载点**，不要传入它的 `documents` 子目录。

如果此前使用 `start.sh` 启动了本机版，先停止它，再启动容器，避免占用同一端口和数据目录：

```sh
systemctl --user stop kindle-desk
```

### 方式 A：使用离线部署包

发布包名为 `kindle-desk-0.2.0-linux-amd64-bundle.tar.gz`，本项目生成的包位于 `dist/`。将它复制到目标 Linux amd64 主机：

```sh
tar -xzf kindle-desk-0.2.0-linux-amd64-bundle.tar.gz
cd kindle-desk-0.2.0-linux-amd64
sha256sum -c SHA256SUMS
docker load -i kindle-desk-0.2.0-linux-amd64.tar.gz
./docker-start.sh "/实际/Kindle挂载点"
```

校验通过并启动成功后，打开 <http://127.0.0.1:8765>。这里的 `docker load` 读取本地镜像文件，启动过程不会拉取镜像或下载转换工具。完整部署包不包含你的图书、摘录或本机 `.env`。

包内文件：

| 文件 | 用途 |
| --- | --- |
| `kindle-desk-0.2.0-linux-amd64.tar.gz` | 供 `docker load` 载入的镜像；它与外层部署包是两个文件 |
| `docker-start.sh`、`compose.yaml` | 启动脚本及容器配置 |
| `README.md`、`.env.example` | 使用说明及手动 Compose 配置示例 |
| `BUILD.txt`、`SHA256SUMS` | 镜像版本、源码提交和文件校验值 |

### 方式 B：在源码目录构建

首次构建需要联网获取 Debian 基础镜像和软件包，构建完成后可离线运行。在含 `Dockerfile` 的源码目录执行：

```sh
docker build --build-arg VERSION=0.2.0 --build-arg VCS_REF="$(git rev-parse HEAD)" -t kindle-desk:0.2.0 .
./docker-start.sh "/实际/Kindle挂载点"
```

若本机已经有 `kindle-desk:0.2.0`，可以直接执行启动脚本。其他 CPU 架构需单独构建、验证；当前离线包不能视为多架构镜像。

### 启动脚本会做什么

`docker-start.sh` 检查挂载点、`documents/` 读写权限、Docker 和本地镜像，使用当前用户的 UID/GID，自动创建部署目录下的 `data/`，然后等待容器健康检查通过。重复运行会**重新创建容器**，以接入当前 USB 挂载；数据目录保留。

脚本不读取 `.env`。自定义数据位置或镜像时，显式传入环境变量：

```sh
KINDLE_DATA="/绝对路径/已有data" KINDLE_IMAGE=kindle-desk:0.2.0 ./docker-start.sh "/实际/Kindle挂载点"
```

`KINDLE_DATA` 默认是脚本目录下的 `data/`；相对路径也以脚本目录为基准。建议使用绝对路径，便于升级时继续使用同一份数据。不要用 `sudo` 启动脚本；应让当前用户有 Docker 使用权限和设备写入权限。

### 日常启停和重新连接

以下命令在包含 `compose.yaml` 的部署目录执行：

```sh
# 查看状态、跟踪日志；退出日志查看不会停止容器。
KINDLE_MOUNT="/实际/Kindle挂载点" docker compose ps
KINDLE_MOUNT="/实际/Kindle挂载点" docker compose logs --tail=100 -f

# 拔线前先停止并移除容器，再从宿主机文件管理器安全弹出。
KINDLE_MOUNT="/实际/Kindle挂载点" docker compose down

# 重新插入并挂载后，再运行启动脚本。
./docker-start.sh "/实际/Kindle挂载点"
```

单个设备目录通过 bind mount 传入容器。设备重新挂载后需要重建容器，单纯刷新网页或重启旧容器不能代替这一步。容器内的“安全弹出”按钮会禁用。

关闭终端不会停止容器；本项目没有配置开机自启，重启主机后应先挂载 Kindle，再运行启动脚本。

### 手动 Compose 配置（可选）

如果希望通过 `.env` 固定配置，可直接使用 Compose：

```sh
cp .env.example .env
id -u
id -g
# 编辑 .env，填入实际挂载路径、上面两项 ID 和数据目录。
mkdir -p data
# 确保镜像已载入或构建，数据目录存在且可写。
docker compose up -d --force-recreate --wait
```

不要覆盖已有 `.env`；首次创建后再编辑。若设置了其他 `KINDLE_DATA`，需要事先创建那个目录。

| 配置项 | 含义 |
| --- | --- |
| `KINDLE_MOUNT` | 宿主机的 Kindle 根挂载点，必须已存在 |
| `LOCAL_UID` / `LOCAL_GID` | 手动 Compose 使用的用户身份，应与设备和数据目录权限匹配 |
| `KINDLE_DATA` | 本地数据目录，默认 `./data` |
| `KINDLE_IMAGE` | 已载入或构建的镜像，当前为 `kindle-desk:0.2.0` |

手动 Compose 会读取 `.env`；`docker-start.sh` 使用参数和环境变量，两种入口不要混淆。旧配置升级到本版时，需要更新 `.env` 中的镜像版本。Compose 不自动构建，也不自动拉取镜像。

容器以普通用户运行，根文件系统只读，只开放设备与数据目录的必要写入，不使用 privileged。端口只发布到 `127.0.0.1:8765`，Host、Origin 和操作令牌校验仍生效；请保持映射两端均为 8765。当前没有面向公网或局域网多用户访问的登录机制。

## 3. 使用图书和摘录管理

### 文件夹分类

1. 在“设备书架”选择“新建分类”，填写单个目录名称并选择上级目录。例如先在根目录创建“文学”，再以“文学”为上级创建“散文”。
2. 从分类下拉框筛选图书。每项显示该目录**直接包含**的书籍，子目录单独列出。
3. 点击书籍旁的“移动”，选择目标分类。也可以把书移回根目录。

上述操作直接修改 Kindle 的 `documents/`。例如 `文学/散文` 对应 `documents/文学/散文`。已有目录和空目录也会显示，断开后可查看最近一次读取的结构。

移动保持书籍文件名不变，并一起移动识别到的同名 `.sdr` 目录及 `.mbp`、`.mbp1`、`.mbs`、`.pdr`、`.apnx`、`.phl` 附件。遇到目标重名，或同名多格式书籍共用附件时会拒绝移动，避免覆盖或拆散文件。

### 传入图书

拖入文件或点击上传区域，选择“传入分类”，然后点击“传入 Kindle”。可选择多个文件，逐本处理；单文件上限为 200 MiB。

- EPUB 使用 calibre 本地转换为 AZW3，转换需要一些时间。
- PDF、AZW3、AZW、MOBI、TXT 直接复制。
- 默认目的地为 `documents/Kindle Desk`，可改成已有分类；同名文件不会覆盖。
- “已复制”表示文件写入完成。整理完后安全弹出，等待 Kindle 更新书架并自行验证能否打开。

“备份文件”下载原始书籍本体，不会去除 DRM，也不包含整个 `.sdr` 附件目录。需要完整设备备份时，应另行复制书籍和配套文件。

### 阅读摘录

连接后点击“刷新设备”，程序读取 `My Clippings.txt` / `我的剪贴.txt`，并尝试读取 `ksdk_annotation_v1.db` 中可识别的内容。也可以点击“导入摘录”手动导入剪贴文件；同一来源中已识别的记录会去重。

使用搜索框、书名筛选和“只看新增”查看摘录。“导出全部”生成 Markdown ZIP，一本书一个文件，解压后可放入 Obsidian。

部分新版标注数据库只保存高亮位置，页面会显示“正文尚未解析”。这是当前解析能力的限制，不代表一定没有高亮。程序先复制设备数据库再读取，不回写设备标注库；已归档记录不会跟随设备端删除。

## 4. 数据、备份与升级

图书保存在 Kindle 上，应用数据保存在宿主机。默认结构如下：

```text
data/
├── desk.sqlite3     # 摘录、离线书架、传输记录和导出状态
├── backups/        # 扫描设备时备份的剪贴文件原件
├── moves/          # 未完成移动的位置记录；成功后移除对应记录
└── calibre-config/ # 转换工具生成的配置（使用后可能出现）
```

`docker compose down`、重建容器和更新镜像都不会删除这个宿主机目录。删除它会丢失本地归档；只备份它也不能备份 Kindle 图书本体。

备份前先停止使用该目录的实例。对于默认 `data/`，在部署目录执行：

```sh
KINDLE_MOUNT="/实际/Kindle挂载点" docker compose down
# 本机版用户则先执行：systemctl --user stop kindle-desk
tar -czf "kindle-data-$(date +%Y%m%d-%H%M%S).tar.gz" data
```

升级或换电脑时，先备份，再导入新镜像，并将**完整数据目录**复制或保留在稳定位置。新部署通过 `KINDLE_DATA` 指向它。恢复备份时先解压到新目录，确认结构后使用；不要让两个实例同时操作同一份数据或设备。

### 移动中断后的处理

多个文件无法在一次原子操作中一起移动。程序会先把原位置和目标位置写入 `data/moves/*.json`；普通失败会尝试回退。若拔线、断电等导致回退失败，记录保留，后续移动被阻止。

出现此提示时，先停止整理并停止应用，重新连接设备，备份涉及的书籍和附件。根据记录中的 `from`、`to` 核对两端，确认文件完整、书籍和附件已在同一目录，且不存在未处理的冲突后，才能清除对应恢复记录并继续。Docker 记录中的 `/kindle` 对应宿主机设置的挂载点；记录本身不包含文件副本。

## 5. Linux 本机运行

在源码目录执行：

```sh
./start.sh
```

脚本需要 Python 3、`systemctl`、`systemd-run`，会启动当前用户的后台服务。重复执行复用已运行实例；关闭终端不会停止服务，重启系统后需重新执行。

```sh
systemctl --user status kindle-desk
journalctl --user -u kindle-desk -n 30 --no-pager
systemctl --user restart kindle-desk
systemctl --user stop kindle-desk
```

默认设备路径是 `/run/media/rose/Kindle`，地址是 <http://127.0.0.1:8765>。自定义路径、端口或数据目录时，先停止已有实例，再以前台方式运行：

```sh
systemctl --user stop kindle-desk
python3 server.py --mount "/实际/Kindle挂载点" --port 8766 --data "/绝对路径/data"
```

此时访问 <http://127.0.0.1:8766>，Ctrl+C 停止。无需 systemd 的环境也可直接运行这个 Python 命令。

本机版的 EPUB 转换需要额外准备 calibre。程序优先使用 `--converter` 指定的路径或 `PATH` 中的 `ebook-convert`，也会查找项目内的 `runtime/calibre/ebook-convert`。本机已有的 `runtime/` 不属于 Git 源码，复制或检出源码不会自动携带转换工具。未安装 calibre 时仍可直接传入其他支持格式。

本机版“安全弹出”依赖宿主机的 `findmnt` 和 `udisksctl`；如果无法确认设备或卸载失败，使用系统文件管理器弹出。

## 6. 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 提示“Kindle 尚未挂载” | 用文件管理器挂载，确认传的是设备根挂载点而非 `documents/`；虚拟机先检查 USB 是否已交给它 |
| 找不到镜像 | 先对包内镜像执行 `docker load -i`，或在源码目录构建；外层 `*-bundle.tar.gz` 应先用 `tar` 解压 |
| 无法写入设备或数据目录 | 检查主机挂载是否只读、目录权限及 UID/GID；容器不会自动修复主机权限 |
| 提示本机版仍在运行或端口占用 | 停止自己启动的旧实例；查看 `systemctl --user status kindle-desk` 和 `docker ps`，不要同时启动两种方式 |
| 重新插入后仍看不到书 | Docker 用户先挂载，再重跑 `docker-start.sh`；本机用户确认路径后点击“刷新设备” |
| 容器健康但网页显示未连接 | 健康检查只说明服务接口可用，不保证 USB、图书扫描或写入成功；检查挂载及页面提示 |
| Windows 浏览器打不开虚拟机的 localhost | 在 Linux 虚拟机内打开该地址；默认没有对外开放访问 |
| EPUB 转换失败 | Docker 用户查看容器日志；本机用户检查 calibre 路径；同时确认文件完整、未加密及磁盘空间充足 |
| 高亮没有正文 | 新版数据库可能只有位置信息；尝试导入含正文的 `My Clippings.txt`，详见已知限制 |
| 提示未完成的移动记录 | 按上面的恢复流程检查实际文件，不要直接清空 `data/moves/` 来跳过检查 |

## 7. 已知限制

- 文件夹分类服务于本网页和磁盘整理，不生成 Kindle 收藏集；分类目前支持新建和移动单本书籍，没有分类重命名、删除或批量移动入口。
- 设备移动后的重索引、阅读进度和不同 KFX 附件关联未作完整实机验证。
- 数据库标注解析不完整，不认识的本地编辑操作会提示并保留旧记录；剪贴文件和数据库的位置体系不同，不跨来源自动合并去重。
- 电量、阅读百分比、当前阅读页面未实现。设备预览不代表正在阅读的书；界面中的型号和固件信息目前针对现有设备配置，不是通用设备探测结果。
- 下载原始书籍不等于移除 DRM，也不保证能在其他阅读器打开。
- Windows 原生启动、MTP 适配、Windows/macOS Docker Desktop 和其他 CPU 架构尚未验证或实现。

## 8. 开发与验证

后端为 Python 标准库 HTTP 服务和 SQLite，前端为静态 HTML/CSS/JavaScript，无需 npm 构建。主要文件：

| 文件 | 职责 |
| --- | --- |
| `server.py` | 设备扫描、摘录、传书、文件夹操作和 HTTP 接口 |
| `web/` | 本地管理页面 |
| `start.sh` / `docker-start.sh` | 本机服务启动 / Docker 启动 |
| `Dockerfile` / `compose.yaml` | 镜像构建 / 运行配置 |
| `test_server.py` / `test_docker_start.py` | 后端及启动脚本测试 |
| `check_docker.py` | 独立容器集成验证 |

```sh
python3 -m unittest -v test_server.py test_docker_start.py
# 先构建 0.2.0 镜像，再执行容器验证；测试需要主机 Python 和 Docker。
python3 check_docker.py
```

测试使用临时目录模拟设备；容器测试使用独立 Compose 项目和随机本机端口，退出时清理测试容器。0.2.0 已通过 21 项单元测试，以及实际 EPUB 转换、文件夹移动、附件保留和重建后数据保留的容器验证；这些结果不能替代真机阅读验证。

镜像不包含 `data/`、`runtime/`、本机 `.env`、Git 历史或测试文件。镜像 OCI 标签和 `BUILD.txt` 的 Git commit 对应应用源码；离线包若另有 Documentation commit，则对应包内说明文档的提交。

参考：[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)、[Docker Desktop USB 限制](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/general/#can-i-pass-through-a-usb-device-to-a-container)。
