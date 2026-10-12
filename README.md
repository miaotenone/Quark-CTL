# 夸克云盘控制工具

`quark_drive.py` 是可独立调用的同步 Python 模块，也提供命令行入口。支持目录列表、搜索、创建目录、移动、重命名、回收站删除、单文件与批量上传下载、创建分享和分享转存。

要求 Python 3.10+。在仓库根目录执行：

```bash
python3 -m pip install -r others/requirements.txt
python3 -m others.quark_drive --help
# 也可使用脚本绝对路径，从其他目录调用：
python3 /path/to/ra-docs-compiler/others/quark_drive.py --help
```

## Cookie 配置

在浏览器登录 `https://pan.quark.cn/`，打开开发者工具的“网络”面板，查看一个发往 `drive-pc.quark.cn` 的已登录请求，复制其请求头 `Cookie` 的值。复制完整的 `name=value; name=value` 内容，不包含 `Cookie:` 前缀。

推荐在本机交互终端执行，按提示粘贴 Cookie（输入不回显）：

```bash
python3 -m others.quark_drive auth-login
python3 -m others.quark_drive auth-status --json
python3 -m others.quark_drive ls --json
```

`auth-login` 不会打开浏览器或扫码；它接收你从浏览器复制的 Cookie，尝试刷新短期会话，验证私人目录访问权限，通过后保存到脚本同目录的 `others/quark.local.json`。该文件已被 Git 忽略，在 Linux/macOS 下权限为 `0600`，仅当前用户可读写。后续命令和 `QuarkClient()` 会自动读取，无需每次粘贴。登录失败不会覆盖原配置。

也可沿用环境变量进行首次配置：

```bash
read -r -s -p 'Quark Cookie: ' QUARK_COOKIE
export QUARK_COOKIE
printf '\n'
python3 -m others.quark_drive auth-refresh --json
```

**凭据优先级已调整为：构造参数 `cookie` → 指定或默认配置文件 → 环境变量。** 环境变量仅在未指定配置路径、默认配置也不存在时使用，避免旧环境变量覆盖已续期的 Cookie。显式指定的配置不存在或无效时直接报错；首次创建自定义路径请用 `--config 路径 auth-login`。更换账号或重新录入凭据请使用 `auth-login`，不要只更新环境变量。

如需手动管理，可复制 `others/quark.example.json` 为 `others/quark.local.json`，在本地填入 Cookie：

```bash
python3 -m others.quark_drive --config others/quark.local.json auth-refresh --json
```

### 会话失效恢复

- 每个客户端首次请求前尝试刷新，后续请求距上次刷新超过 100 分钟时再次刷新。无需调用者定时调用；空闲期间不启动后台线程。
- 刷新时移除短期 `__puus`，保留其他登录凭据，请求新的会话，再读取私人目录验证登录。新 Cookie 使用临时文件原子替换本地配置，重启脚本后继续使用。
- 服务端没有下发新会话时，恢复原 Cookie 并验证是否仍能访问私人目录。若仍有效，允许操作继续，但返回 `refreshed: false`，后续最早在 5 分钟后再次尝试，不把验证通过当成续期成功。
- 普通 API 响应中轮换的 Cookie 也会保存。保存失败通过 `credential_save_error`（Python）和标准错误（命令行）报告，不把已经完成的云盘操作改判为失败；应解决文件权限或配置冲突后再续期。
- 读取请求遇到明确的登录失效会刷新并重试一次。下载返回 403 时重新刷新、获取下载链接，再下载一次；签名链接与请求 Cookie 始终使用同一快照。
- 创建、移动、删除、上传预处理和转存等写操作不会自动重放。若提交时失效，成功刷新后仍提示确认远程结果，再由调用者决定是否重试。
- **长期凭据被撤销或账号登录已失效时，无法仅靠刷新恢复。** 脚本会提示运行 `auth-login`；此时先在浏览器重新登录，再复制新 Cookie。也不保证永久免登录。

主动检查与刷新：

```bash
python3 -m others.quark_drive auth-status --json
python3 -m others.quark_drive auth-refresh --json
```

如果需要在没有文件操作的期间持续尝试续期，可运行前台保活进程，按 Ctrl+C 停止：

```bash
python3 -m others.quark_drive auth-keepalive --json
```

保活默认每 6000 秒刷新一次，每次输出一行不含 Cookie 的状态 JSON；没有新会话时缩短到最多 300 秒再次检查。可用 `--interval 秒数` 缩短周期。网络重试耗尽、登录失效或保存失败时，进程以非零状态退出；不会无限掩盖错误。**同一个配置文件仅支持一个写入进程**，运行其他 CLI 命令前先停止该保活进程；通常直接依赖各操作内置的刷新即可。

Python 中也可主动调用或禁用自动功能：

```python
with QuarkClient() as client:
    status = client.refresh_session()  # 不含 Cookie，包含 authenticated/refreshed/persisted
    status = client.auth_status()
    if client.credential_save_error:
        print(client.credential_save_error)

# 由上层程序自行管理会话及凭据时使用。
with QuarkClient(cookie=my_cookie, auto_refresh=False, persist_cookie=False) as client:
    files = client.list_files()
```

命令行对应选项为 `--no-auto-refresh`、`--no-save-cookie`，放在子命令之前。`--no-save-cookie` 只禁止写入，不改变凭据读取顺序。`refresh_interval` 构造参数控制请求触发刷新的间隔（秒）；`auth-refresh` 和 `auth-login` 总会执行显式刷新，不受 `--no-auto-refresh` 影响。

## 在其他 Python 文件中调用

从仓库根目录运行调用者，或将仓库根目录加入调用者的 `PYTHONPATH`。模块导入不会读取凭据、发出请求或打印输出。建议使用上下文管理器关闭网络连接；一个实例按顺序调用，不在线程间共享。

```python
from others.quark_drive import QuarkClient, QuarkError, TaskTimeoutError

with QuarkClient() as client:
    # 使用已有文件夹 ID；根目录 ID 为字符串 "0"。
    entries = client.list_files(folder_id="0")
    for item in entries:
        print(item["fid"], item["file_name"], item.get("size"))

    matches = client.search_files("报告")
    folder = client.create_folder("测试目录", parent_id="0")
    uploaded = client.upload_file("report.pdf", parent_id="目标目录ID")
    downloaded = client.download_file(uploaded["fid"], output_dir="others/downloads")

    client.rename_file(uploaded["fid"], "报告.pdf")
    client.move_files([uploaded["fid"]], target_folder_id="其他目录ID")
    shared = client.create_share([uploaded["fid"]], title="报告", expires="7d", password=True)
    print(shared["share_url"], shared.get("passcode"))

    shared_files = client.list_share_files("https://pan.quark.cn/s/分享ID", passcode="提取码")
    saved = client.save_share(
        "https://pan.quark.cn/s/分享ID", parent_id="目标目录ID",
        passcode="提取码", file_ids=[shared_files[0]["fid"]],
    )
    # 不传 file_ids 时，保存当前分享目录的全部条目（包括目录）。
    # 如需查看分享子目录，传 folder_id；转存时对应传 share_folder_id。

    results = client.upload_files(["a.pdf", "b.pdf"], parent_id="目标目录ID")
    results = client.download_files(["文件ID1", "文件ID2"], output_dir="others/downloads")
    for item in results:
        if not item["ok"]:
            print(item["input"], item["error"])

    # 下面的显式调用会将文件移入回收站。
    client.delete_files(["待删除的文件ID"])
```

上面的操作是独立用法示例，须将示例文件和 ID 替换为自己的实际值。也可使用 `QuarkClient(cookie=...)` 或 `QuarkClient(config_file=...)`。

### 返回值及错误

| 方法 | 返回值 |
| --- | --- |
| `list_files`、`search_files`、`list_share_files` | 全部分页的条目列表，保留接口字段，例如 `fid`、`file_name`、`size`、`file` |
| `create_folder`、`move_files`、`rename_file`、`delete_files`、`save_share` | 接口业务数据字典；存在异步任务时返回完成任务的数据 |
| `create_share` | 包含 `share_url` 的字典；有提取码时保留服务端相关字段 |
| `upload_file` | `fid`、`file_name`、`size`、`md5`、`instant` |
| `download_file` | `fid`、本地绝对路径 `path`、`size`、`md5` |
| `upload_files`、`download_files` | 逐项返回 `input`、`ok`，以及成功的 `data` 或失败的 `error` |
| `wait_task(task_id)` | 确认成功的任务数据；可查询此前已提交的任务 |
| `refresh_session()` | 登录、刷新、落盘状态，不包含 Cookie；保存失败时有 `warning` |
| `auth_status()` | 验证登录状态及当前自动刷新配置，不包含 Cookie |

单次调用失败会抛出 `QuarkError`（包括 `AuthenticationError` 和 `TaskTimeoutError`）；本地路径、同名冲突和参数错误分别使用 `OSError` 的子类或 `ValueError`。批量调用收集这些错误并继续后续文件，调用者应检查每项 `ok`。

默认单个请求超时 30 秒，异步任务等待 120 秒，轮询间隔 1 秒，可在构造函数中通过 `timeout`、`task_timeout`、`poll_interval` 设置。任务等待期限可能额外包含最后一次请求的耗时及有限重试等待。任务超时不表示远程任务已取消；应先查询云盘或调用 `wait_task`，避免重复提交。

## 命令行

下面命令均在仓库根目录执行。文件和目录均使用 ID，目录 ID 可通过 `ls` 获得；不将远程路径自动解析为 ID。

```bash
python3 -m others.quark_drive ls --folder-id 0 --json
python3 -m others.quark_drive search '报告' --json
python3 -m others.quark_drive mkdir '资料' --parent-id 0
python3 -m others.quark_drive move 文件ID1 文件ID2 --parent-id 目标目录ID
python3 -m others.quark_drive rename 文件ID '新名称.pdf'
python3 -m others.quark_drive delete 文件ID1 文件ID2
python3 -m others.quark_drive upload report.pdf other.pdf --parent-id 目标目录ID --json
python3 -m others.quark_drive download 文件ID1 文件ID2 --output-dir others/downloads --json
python3 -m others.quark_drive download 文件ID --output-dir others/downloads --overwrite
python3 -m others.quark_drive share 文件ID --title '资料' --expires 7d --password --json
python3 -m others.quark_drive share-ls 'https://pan.quark.cn/s/分享ID' --passcode 提取码
python3 -m others.quark_drive save 'https://pan.quark.cn/s/分享ID' --passcode 提取码 --parent-id 目标目录ID
python3 -m others.quark_drive save 'https://pan.quark.cn/s/分享ID' --file-ids 分享文件ID --parent-id 目标目录ID
python3 -m others.quark_drive task 任务ID --json
```

`--config`、`--timeout` 和 `--task-timeout` 放在子命令之前；`--json` 可在子命令前或后。分享有效期支持 `forever`、`1d`、`7d`（默认）、`30d`；`--password` 请求服务器生成提取码。分享链接支持 `?pwd=...` 参数，显式 `--passcode` 优先。

标准输出只包含结果 JSON；默认缩进显示，`--json` 使用单行格式。整体错误和批量失败提示写入标准错误。退出码：`0` 成功、`1` 操作失败或批量部分失败、`2` 命令行参数错误、`130` 用户中断。作为子进程调用时应同时检查退出码和批量逐项结果。

## 传输及操作边界

- 上传前计算 MD5/SHA1，支持秒传及分片上传。普通上传依次完成分片、合并及完成通知，再检查目录中的文件是否可见。内存用量受分片大小约束，不会整体加载大文件。
- 上传前拒绝目标目录已有的同名条目；没有远程覆盖选项。需要更新文件时，可使用 `upload_file(..., remote_name="新名称.pdf")`。此检查不是服务端原子锁，请勿并发向同一目录上传同名文件。
- 下载流式写入同目录临时文件，核对可用的大小及 MD5 信息后发布；中断时清理临时文件。已有本地文件默认拒绝覆盖，显式 `overwrite=True` 或 `--overwrite` 才替换。
- 下载拒绝路径穿越、符号链接目标，以及非夸克相关 HTTPS 下载域名。当前不会自动跟随 HTTP 重定向；如上游改用其他下载域名或跳转机制，会明确报错，需适配后使用。
- 删除只提供移入回收站。显式执行 `delete` 或 `delete_files` 直接生效，不进行交互确认。
- 只为读取请求和同一上传分片做有限重试；登录失效后的恢复同样不自动重放创建、移动、分享、转存或上传预处理请求。
- 不支持本地目录递归上传、云盘目录递归下载、同步、断点续传、多账号调度及账号权限之外的传输能力。转存分享中的目录由云盘服务端完成。

## 测试与真实账号验收

离线测试不需要 Cookie，也不会连接云盘：

```bash
python3 -m unittest discover -s others/tests -v
```

当前交付已通过 52 项离线测试，包含过期恢复、刷新失败回滚、凭据保存及下载签名一致性；由于实现环境未配置 Cookie，**真实账号联调尚未完成**。接口依赖网页登录会话，服务端变化、账号权限、容量或下载限制可能导致调用失败。

本地配置有效 Cookie 后，在专用测试目录依次检查：

1. 运行 `auth-login`，确认 `auth-status` 成功；退出并重开终端，确认无需环境变量也能访问。执行 `auth-refresh` 检查 `refreshed`、`persisted`，不要输出凭据。随后列出根目录并创建唯一命名的测试目录，记录返回的 ID。
2. 上传小文件和大于 4 MiB 的文件，下载后用 `sha256sum` 比较原文件与下载文件。
3. 验证同名上传拒绝、重复下载拒绝及显式覆盖。
4. 创建第二个测试目录，验证重命名及移动。
5. 创建带提取码的分享，通过 `share-ls` 查看，再转存到第二个目录并确认文件。
6. 显式删除测试文件和测试目录，在网页回收站确认；不要清空回收站。

协议核对参考：[AList 文件操作](https://github.com/AlistGo/alist/blob/main/drivers/quark_uc/driver.go)、[AList 传输协议](https://github.com/AlistGo/alist/blob/main/drivers/quark_uc/util.go)、[分享转存接口用例](https://github.com/Pearsoon/quark/blob/master/quark.py)。这些是第三方客户端实现，并非官方稳定 API 承诺。本工具独立组织客户端、错误处理、文件安全检查与测试，不安装上述项目作为依赖。
