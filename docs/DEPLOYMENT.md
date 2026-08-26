# 部署约定

## Build

```bash
docker build -t sub-manager:latest .
```

## Run

```bash
mkdir -p /opt/sub-manager/data

docker run -d \
  --name sub-manager \
  --restart unless-stopped \
  -p 127.0.0.1:7777:7777 \
  -e TZ='Asia/Shanghai' \
  -v /opt/sub-manager/data:/data \
  sub-manager:latest
```

如果需要先通过 IP:7777 调试，可临时使用：

```bash
-p 7777:7777
```

## 1Panel / Nginx

反代：

```text
http://127.0.0.1:7777
```

建议 HTTPS 域名：

```text
https://sub.example.com
```

正式域名在 Web 后台设置，不应要求重建容器。

## 数据

唯一需要长期备份：

```text
/opt/sub-manager/data
```

升级镜像时不能删除这个目录。
