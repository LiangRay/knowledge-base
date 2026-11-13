# S3 视频在线播放：手动配置手册（公开桶版 + 格式兼容性实测）

> 目的：把视频放在 S3 上，别人拿到链接就能在浏览器或手机里**直接播放**，不用下载。
> 本文是**控制台手动操作**步骤，每一步都附 CLI 等价命令。内容已在 us-west-2 实测验证。
> 文中的 `<BUCKET>`、`<REGION>` 都是占位符，请替换成你自己的值。

---

## 0. 一句话结论

- **S3 本身就能在线播放**：对象的 Content-Type 设对，再让浏览器能访问到，`<video>` 或浏览器地址栏就能直接播，拖动进度条也行（S3 支持 Range 请求）。
- **能不能播看编码，不看后缀**：**H.264 + AAC 的 MP4** 所有设备都能播，首选。HEVC、AVI、FLV 不建议直接放。
- **公开桶只适合测试和小流量场景**。正式对外或流量大时，改用 **CloudFront + OAC + 私有桶**（见第 7 节）。

---

## 1. 方案选择

| 方案 | 访问方式 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|---|
| A. 预签名 URL | 私有桶 + 带签名的临时链接 | 不用开公开访问，最安全、最快 | 链接最长有效 7 天；链接很长 | 临时发给几个人看 |
| **B. 公开桶（本文）** | 固定 URL，任何人可访问 | 链接永久有效，配置简单 | 谁都能下载；流量费不可控 | 测试、内部演示、公开素材 |
| C. CloudFront + OAC | CDN 域名，桶保持私有 | 便宜、快，可加防盗链和 WAF | 配置多几步 | 正式对外 |
| D. HLS + CloudFront | 转码成 m3u8 切片 | 自适应码率，适合长视频 | 需要 MediaConvert 转码 | 长视频、移动网络 |

---

## 2. 准备视频（最关键的一步）

### 2.1 推荐格式

- 容器：**MP4**
- 视频编码：**H.264（High profile，yuv420p）**
- 音频编码：**AAC**
- **faststart**：moov 元数据放在文件头，浏览器不用下载完就能开播

### 2.2 转换命令（ffmpeg）

```bash
# 任意格式 → 浏览器通用 MP4
ffmpeg -i input.xxx \
  -c:v libx264 -pix_fmt yuv420p -preset medium -crf 23 \
  -c:a aac -b:a 128k \
  -movflags +faststart \
  output.mp4

# 已经是 H.264 MP4，只是没做 faststart：不重新编码，几秒完成
ffmpeg -i input.mp4 -c copy -movflags +faststart output.mp4
```

### 2.3 检查视频

```bash
# 看编码：应该输出 h264 / aac
ffprobe -v error -show_entries stream=codec_name,pix_fmt -of csv=p=0 output.mp4

# 看 faststart：moov 应该出现在 mdat 前面
python3 - <<'EOF'
import struct
f = open("output.mp4", "rb"); pos = 0
while True:
    h = f.read(8)
    if len(h) < 8: break
    size, typ = struct.unpack(">I4s", h)
    print(typ.decode(), "@", pos)
    if size == 1: size = struct.unpack(">Q", f.read(8))[0]
    pos += size; f.seek(pos)
EOF
```

---

## 3. 创建 S3 桶（控制台）

1. 打开 **S3 控制台 → Buckets → Create bucket**
2. **Bucket name**：`<BUCKET>`（全局唯一，建议加随机后缀）
3. **AWS Region**：选离观众近的 Region
4. **Object Ownership**：保持 **ACLs disabled（Bucket owner enforced）**
5. **Block Public Access settings**：先保持默认全部勾选，第 4 步再改
6. 其余保持默认 → **Create bucket**

CLI 等价命令：

```bash
aws s3api create-bucket --bucket <BUCKET> --region <REGION> \
  --create-bucket-configuration LocationConstraint=<REGION>
# us-east-1 不要带 --create-bucket-configuration
```

---

## 4. 放开公开读（只开一个目录）

### 4.1 先检查账号级的 Block Public Access

**S3 控制台 → 左侧 Block Public Access settings for this account**

- 如果这里是 **On**，桶级怎么改都没用，要先在这里关掉，或者找管理员。
- 在 Organizations 下的账号，还可能被 **SCP** 禁止 `s3:PutBucketPolicy` 或公开访问，遇到报错要找组织管理员。

```bash
aws s3control get-public-access-block --account-id <ACCOUNT_ID>
# 返回 NoSuchPublicAccessBlockConfiguration = 账号级未配置，不会拦截
```

### 4.2 桶级 Block Public Access：只关两项

进入桶 → **Permissions → Block public access (bucket settings) → Edit**

| 选项 | 设置 | 原因 |
|---|---|---|
| Block public access to buckets and objects granted through **new ACLs** | ✅ 保持勾选 | 不用 ACL |
| … through **any ACLs** | ✅ 保持勾选 | 不用 ACL |
| … through **new public bucket or access point policies** | ⬜ **取消** | 允许写公开桶策略 |
| … through **any public bucket or access point policies** | ⬜ **取消** | 允许公开策略生效 |

保存时输入 `confirm`。

```bash
aws s3api put-public-access-block --bucket <BUCKET> \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
```

### 4.3 桶策略：只开放 `videos/` 目录的读取

**Permissions → Bucket policy → Edit**，粘贴：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadVideosOnly",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<BUCKET>/videos/*"
    }
  ]
}
```

要点：
- **只给 `s3:GetObject`**，不给 `s3:ListBucket`，这样别人不能列出桶里的文件。
- **Resource 限定在 `videos/*`**，桶里其它目录仍然是私有的。

```bash
aws s3api put-bucket-policy --bucket <BUCKET> --policy file://policy.json
```

> 保存后，桶名旁会显示红色的 **Publicly accessible** 标记，这是正常的。

---

## 5. 上传视频（一定要设对 Content-Type）

### 5.1 控制台

1. 进入桶 → **Create folder** → 新建 `videos`
2. 进入 `videos/` → **Upload → Add files**
3. 展开 **Properties → Metadata → Add metadata**
   - Type：**System defined**
   - Key：**Content-Type**
   - Value：按下表填写
4. **Upload**

控制台通常会按后缀自动识别，但 `.mkv`、`.flv` 等可能被识别成 `binary/octet-stream`。这时浏览器会**直接下载而不播放**，上传后一定要检查。

已经上传的文件可以这样改：选中对象 → **Actions → Edit metadata**。

### 5.2 Content-Type 对照表

| 后缀 | Content-Type |
|---|---|
| .mp4 | `video/mp4` |
| .webm | `video/webm` |
| .mov | `video/quicktime` |
| .mkv | `video/x-matroska` |
| .m3u8 | `application/vnd.apple.mpegurl` |
| .ts | `video/mp2t` |
| .html | `text/html; charset=utf-8` |

### 5.3 CLI

```bash
aws s3 cp demo.mp4 s3://<BUCKET>/videos/demo.mp4 --content-type video/mp4

# 批量上传整个目录（CLI 按后缀猜类型，传完建议抽查一下）
aws s3 sync ./videos s3://<BUCKET>/videos/
```

---

## 6. 验证

### 6.1 播放地址

```
https://<BUCKET>.s3.<REGION>.amazonaws.com/videos/demo.mp4
```

控制台里点对象 → **Object URL** 就是这个地址。浏览器直接打开就能播放，也可以嵌到网页里：

```html
<video src="https://<BUCKET>.s3.<REGION>.amazonaws.com/videos/demo.mp4"
       controls playsinline preload="metadata" width="720"></video>
```

> iPhone 上要内嵌播放（不自动全屏）需要加 `playsinline`；如果要自动播放，还得同时加 `muted autoplay`。

### 6.2 curl 检查清单（实测结果）

| 检查项 | 命令 | 期望结果 |
|---|---|---|
| 可公开访问、类型正确 | `curl -I <URL>` | `200`、`Content-Type: video/mp4`、`Accept-Ranges: bytes` |
| 支持拖动进度条 | `curl -r 0-1023 -o /dev/null -w '%{http_code}' <URL>` | `206` |
| 其它目录仍私有 | `curl -I https://<BUCKET>.s3.<REGION>.amazonaws.com/other.txt` | `403` |
| 不能列出桶内文件 | `curl -I https://<BUCKET>.s3.<REGION>.amazonaws.com/` | `403` |
| 远程从中间截帧（验证 Range + faststart） | `ffmpeg -ss 3 -i <URL> -frames:v 1 frame.jpg` | 能很快出图 |

### 6.3 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 打开后直接下载，不播放 | Content-Type 是 `binary/octet-stream` 或 `application/octet-stream` | Edit metadata 改成 `video/mp4` |
| 403 AccessDenied | 桶级或账号级 Block Public Access 没关；策略里的 Resource 路径写错 | 检查 4.1–4.3 |
| 保存策略时报 Access denied | 账号级 BPA 开着，或被 SCP 拦了 | 找管理员 |
| 有声音没画面 | 视频是 HEVC 或其它编码，当前浏览器解不了 | 转成 H.264（2.2 节） |
| 要等很久才开播 | 没做 faststart，moov 在文件尾 | `ffmpeg -c copy -movflags +faststart` |
| 中国大陆访问慢或不稳定 | 海外 Region 直连 | 用 CloudFront，或者把视频放到中国区 Region |
| 网页里用 JS 读取视频（hls.js、canvas）报 CORS 错误 | 跨域 | 配置桶的 CORS（见下方） |

CORS 只在跨域用 JS 读取视频时需要，普通 `<video src>` 播放不需要。配置位置：**Permissions → CORS**：

```json
[
  {
    "AllowedOrigins": ["https://your-site.example.com"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["Content-Length", "Content-Range", "Accept-Ranges"],
    "MaxAgeSeconds": 3600
  }
]
```

---

## 7. 视频格式兼容性（实测）

测试方法：用 ffmpeg 生成同一段 720p 测试视频，编码成 9 种格式，按正确的 Content-Type 上传到公开桶，再用测试页（[assets/format-test.html](assets/format-test.html)）逐个加载。

测试环境：Linux Headless Chrome 147（无 GPU）。

| 格式 | 编码 | 实测结果 | 其他浏览器（经验值，未实测） | 建议 |
|---|---|---|---|---|
| **MP4** | **H.264 + AAC** | ✅ 正常 | 全平台通用 | **首选** |
| MOV | H.264 + AAC | ✅ 正常 | Safari / Chrome 可以，Firefox 不稳定 | 改封装成 MP4 |
| MKV | H.264 + AAC | ✅ 正常 | Chrome 可以，Safari 不支持 | 改封装成 MP4 |
| WebM | VP9 + Opus | ✅ 正常 | Chrome / Firefox / Edge 可以，较新的 Safari 支持 | 可以作为备选源 |
| WebM | VP8 + Opus | ✅ 正常 | 同上 | 可以作为备选源 |
| MP4 | AV1 + AAC | ✅ 正常 | 新浏览器可以，老设备不支持 | 省带宽，需配 H.264 兜底 |
| MP4 | **H.265 / HEVC** | ⚠️ **有声音没画面**（视频尺寸 0x0） | Safari / iOS 可以；Win/Mac Chrome 依赖硬件解码 | **转 H.264** |
| AVI | MPEG-4 Part 2 + MP3 | ❌ error code 4 | 浏览器都不支持 | **转 H.264** |
| FLV | Sorenson + MP3 | ❌ error code 4 | Flash 已淘汰 | **转 H.264** |

结论：
1. **能不能播由编码决定，后缀不说明问题**。同样是 `.mp4`，H.264 能播，HEVC 不一定。
2. 封装不同（MOV/MKV）但编码是 H.264 的，通常不用重新编码，`ffmpeg -i in.mkv -c copy -movflags +faststart out.mp4` 就能转成 MP4。
3. 想兼顾新编码和兼容性，可以在 `<video>` 里放多个源，浏览器会按顺序挑第一个能播的：

```html
<video controls playsinline>
  <source src="demo.av1.mp4" type='video/mp4; codecs="av01.0.05M.08, mp4a.40.2"'>
  <source src="demo.webm"    type='video/webm; codecs="vp9, opus"'>
  <source src="demo.mp4"     type='video/mp4; codecs="avc1.640028, mp4a.40.2"'>
</video>
```

4. 来源格式很杂的话，可以用 **S3 上传事件 → Lambda → MediaConvert** 统一转成 MP4 或 HLS。

---

## 8. 正式环境建议：CloudFront + OAC（桶保持私有）

公开桶的风险：
- 谁都能下载
- **按 S3 公网出流量计费**，被盗链或刷流量时费用不可控
- 没有 CDN 缓存

正式对外建议改成下面的配置，控制台要点如下：

1. 桶保持 **Block Public Access 全部开启**，删掉第 4.3 节的公开策略
2. **CloudFront → Create distribution**
   - Origin domain：选这个 S3 桶
   - Origin access：选 **Origin access control settings (recommended)** → Create new OAC
   - Viewer protocol policy：**Redirect HTTP to HTTPS**
   - Cache policy：`CachingOptimized`
3. 创建完成后，按控制台提示**复制桶策略**，粘贴到 S3 桶策略里。这个策略只允许这个 Distribution 访问
4. 播放地址变成 `https://<DIST_DOMAIN>/videos/demo.mp4`
5. 可选的加固措施：
   - 签名 URL 或签名 Cookie（防盗链）
   - 地理限制
   - WAF 限速
   - 自定义域名 + ACM 证书

> CloudFront 默认支持 Range 请求，拖动进度条没问题。到 CloudFront 的回源流量免费，出流量单价比 S3 直连低，还有免费额度。

---

## 9. 成本参考

| 项 | 说明 |
|---|---|
| S3 存储 | 按 GB 每月计费，视频体积大可以配生命周期规则转到 IA 存储层 |
| GET 请求 | 拖动进度条会产生多次 Range GET，单价很低 |
| **出流量** | **成本大头**。例如 100MB 的视频被看 1000 次 ≈ 100GB 出流量 |
| CloudFront | 有每月免费额度；正式环境通常比 S3 直连便宜 |

> 建议配一个 **AWS Budgets** 预算报警，防止公开链接被刷流量。

---

## 10. 清理

**控制台**

- 进入桶 → **Empty**（输入 `permanently delete`）
- 回到桶列表 → **Delete**（输入桶名）

**CLI**

```bash
aws s3 rb s3://<BUCKET> --force   # 清空并删除桶
```

如果开了版本控制，Empty 时会一并删除所有版本。

---

## 附：实测记录摘要

| 项 | 值 |
|---|---|
| Region | us-west-2 |
| 测试视频 | testsrc2 1280x720 30fps + 440Hz 正弦音，H.264 High + AAC 96k，faststart |
| Block Public Access | BlockPublicAcls=true, IgnorePublicAcls=true, BlockPublicPolicy=false, RestrictPublicBuckets=false |
| 桶策略 | 仅 `s3:GetObject` on `videos/*` |
| HEAD | 200 / video/mp4 / Accept-Ranges: bytes |
| Range 请求 | 206 |
| 访问 `videos/` 以外的对象 / 列桶 | 403 / 403 |
| faststart | moov@36、mdat@12001 |
| 远程截第 3 秒的帧 | 成功 |
