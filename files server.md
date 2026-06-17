# Object Server 使用文档

## 1. 简介
Object Server 提供文件上传与下载服务，特点：
- 需要 `X-API-Key` 才能上传
- 额外提供一个无需 `API_KEY` 的图片/视频上传接口
- 允许任意文件类型
- `POST /upload` 正式支持 Excel/CSV 文件：`.xlsx`、`.xls`、`.csv`
- 返回永久链接 `/files/{code}`
- PDF 自动生成首页预览图，图片文件可直接预览
- 普通图片会额外生成查看优化图，加快内网浏览速度
- 普通视频会额外生成高清播放优化版，加快内网首开和拖动速度
- 预览链接 `/images/{code}`
- `/images/{code}` 会对已生成的预览图、查看优化图、优化视频使用强缓存
- 文件存本地 `files/` 目录
- SHA-256 去重
- 写入 PostgreSQL（文件元数据 + bookid 关联）

## 2. 运行方式（systemd）
已配置 systemd 服务：`object-server.service`

常用命令：
```bash
sudo systemctl status object-server --no-pager -l
sudo systemctl restart object-server
sudo journalctl -u object-server --no-pager -n 100
```

## 3. 环境变量
- `PORT`：服务端口（默认 3000）
- `API_KEY`：上传鉴权 key（用于 `X-API-Key`）
- `DATABASE_URL`：PostgreSQL 连接串
- `STORAGE_DIR`：文件存储目录（默认 `./files`）
- `PREVIEW_DIR`：预览图目录（默认 `${STORAGE_DIR}/previews`）
- `DERIVED_DIR`：普通图片查看图目录（默认 `${STORAGE_DIR}/derived`）
- `VIDEO_DERIVED_DIR`：普通视频播放优化版目录（默认 `${STORAGE_DIR}/video-derived`）
- `VIEW_MAX_DIMENSION`：查看图最长边（默认 `1600`）
- `VIEW_JPEG_QUALITY`：JPEG 查看图质量（默认 `82`）
- `VIDEO_MAX_WIDTH`：视频优化版最大宽度（默认 `1920`）
- `VIDEO_MAX_HEIGHT`：视频优化版最大高度（默认 `1080`）
- `VIDEO_CRF`：视频清晰度与体积平衡参数（默认 `22`）
- `VIDEO_PRESET`：视频转码速度预设（默认 `veryfast`）
- `VIDEO_AUDIO_BITRATE`：视频音频码率（默认 `128k`）
- `BASE_URL`：固定外网域名（默认 `https://files.my365biz.com`）
- `UPLOAD_ALLOWED_KINDS`：可选，限制 `POST /upload` 允许的文件种类，支持 `file,pdf,image,video,spreadsheet`

## 4. 上传接口
`POST /upload`

### 请求头
- `X-API-Key: <key>`

### 表单字段（multipart/form-data）
- `file`：文件
- `bookid`：业务 ID

Excel/CSV 正式支持：
- `.xlsx`
- `.xls`
- `.csv`

### 返回
```json
{ "code": "<code>", "link": "http://host/files/<code>", "imageUrl": "http://host/images/<code>" }
```
`imageUrl` 在上传文件为 PDF、图片或视频时返回。
其中：
- PDF 返回首页预览图
- 普通图片返回查看优化图，不是原始大图
- 普通视频返回高清播放优化版，不是原始视频
- Excel/CSV 只返回 `code` 和 `link`，不返回 `imageUrl`

### 示例
```bash
curl -X POST http://localhost:3000/upload \
  -H "X-API-Key: <your_key>" \
  -F "bookid=12345" \
  -F "file=@/path/to/file"
```

Excel 示例：
```bash
curl -X POST http://localhost:3000/upload \
  -H "X-API-Key: <your_key>" \
  -F "bookid=12345" \
  -F "file=@/path/to/report.xlsx;type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
```

如果希望 `/upload` 只接 Excel/CSV，可设置：
```bash
UPLOAD_ALLOWED_KINDS=spreadsheet
```

## 4.1 图片/视频上传接口（无需 API Key）
`POST /image`

### 表单字段（multipart/form-data）
- `file`：必填，仅支持图片或视频
- `bookid`：可选

### 返回
```json
{ "code": "<code>", "link": "http://host/files/<code>", "imageUrl": "http://host/images/<code>" }
```

说明：
- 图片的 `imageUrl` 返回查看优化图
- 视频的 `imageUrl` 返回可直接内联播放的高清优化版视频地址

### 示例
```bash
curl -X POST http://localhost:3000/image \
  -F "file=@/path/to/photo-or-video"
```

## 5. 下载接口
`GET /files/{code}`

说明：
- Excel/CSV 与其它非预览类文件一样，经由此接口下载
- 下载文件名取自上传时的原始文件名或之后 `/rename` 更新后的文件名

### 示例
```bash
curl -O http://localhost:3000/files/<code>
```

## 6. 预览接口
`GET /images/{code}`

说明：
- 若为 PDF，返回首页预览图（PNG）
- 若为图片文件（`image/*`），优先返回查看优化图
- 若为视频文件（`video/*`），优先返回可直接播放的高清优化版视频流
- 若本地已存在预览图、图片查看图或视频优化版，会直接从本地衍生文件返回，不再先查数据库
- 返回 `Cache-Control: public, max-age=31536000, immutable`、`ETag` 和 `Last-Modified`
- 原始文件请使用 `/files/{code}`

### 示例
```bash
curl -O http://localhost:3000/images/<code>
```

## 6.1 健康检查
`GET /health`

返回服务和数据库状态：
```json
{ "ok": true, "database": true }
```

## 7. 数据库表
### files
- `code`：唯一文件码
- `hash`：SHA-256
- `original_name`：原始文件名
- `content_type`：MIME
- `size_bytes`：大小
- `storage_path`：本地路径
- `created_at`

### file_books
- `hash`
- `bookid`
- `created_at`

(唯一约束：`(hash, bookid)`)

## 8. 重命名接口
`POST /rename`

### 请求头
- `X-API-Key: <key>`

### 请求体（JSON）
任选其一传 `fileUrl/url` 或 `code`：
```json
{ "fileUrl": "http://host/files/<code>", "newName": "example.pdf" }
```
```json
{ "code": "<code>", "newName": "example.pdf" }
```

说明：
- 只更新数据库里的 `original_name` 字段，不改磁盘文件名。

### 返回
```json
{ "code": "<code>", "link": "http://host/files/<code>" }
```

### 示例
```bash
curl -X POST http://localhost:3000/rename \
  -H "X-API-Key: <your_key>" \
  -H "Content-Type: application/json" \
  -d '{"fileUrl":"http://localhost:3000/files/<code>","newName":"new-name.pdf"}'
```

## 9. Contabo 异地备份

仓库内提供了一个把本机 `files/` 和 PostgreSQL 备份到 Contabo Object Storage 的脚本：

- 脚本：[`scripts/backup-to-contabo.sh`](./scripts/backup-to-contabo.sh)
- systemd service：[`systemd/object-server-backup.service`](./systemd/object-server-backup.service)
- systemd timer：[`systemd/object-server-backup.timer`](./systemd/object-server-backup.timer)
- 环境变量示例：[`scripts/backup-to-contabo.env.example`](./scripts/backup-to-contabo.env.example)

默认行为：
- 每天凌晨自动执行一次
- 先同步本地 `files/` 目录到对象存储
- 再执行 `pg_dump` 并上传数据库备份
- 数据库备份默认保留最近 30 天
- 数据库备份默认是非严格模式：文件同步成功就算本次任务成功，数据库失败只记日志

你需要准备的参数：
- `BACKUP_RCLONE_BUCKET`
- `BACKUP_S3_ENDPOINT`
- `BACKUP_S3_ACCESS_KEY_ID`
- `BACKUP_S3_SECRET_ACCESS_KEY`
- `BACKUP_DATABASE_URL`
- `BACKUP_STRICT_DB_BACKUP`：设为 `1` 时，数据库备份失败会让任务返回失败

Contabo Object Storage 使用 S3 风格 API。官方文档里也推荐了 `rclone` 和 `aws cli` 作为工具。

### 启用方式
```bash
sudo cp /etc/object-server-backup/backup.env.example /etc/object-server-backup/backup.env
sudoedit /etc/object-server-backup/backup.env
sudo systemctl enable --now object-server-backup.timer
```

手动试跑一次：
```bash
sudo systemctl start object-server-backup.service
sudo journalctl -u object-server-backup.service -n 100 --no-pager
```
