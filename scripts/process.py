#!/usr/bin/env python3
"""批量下载 zip -> 解压 -> 图片转 WebP -> 重新打包。

示例:
    python scripts/process.py --urls urls.txt --output output --quality 80
    python scripts/process.py --urls urls.txt --max-size 1600 --keep-originals
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests
from PIL import Image, ImageOps

# 支持的图片扩展名(含 .webp,便于对已有 webp 重新按 quality/max-size 编码)
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
USER_AGENT = "Mozilla/5.0 (compatible; GitHubActions-WebPBatch/1.0)"


def set_github_output(key: str, value: str) -> None:
    """把统计结果写进 GitHub Actions 的 step output,供后续步骤做条件判断。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def download(url: str, dest: Path, timeout: int = 120, retries: int = 3) -> None:
    """流式下载到本地文件,先写 .part 再原子替换,避免半成品被当成完整包。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    headers = {"User-Agent": USER_AGENT}
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        print(f"[下载] {url} (第 {attempt}/{retries} 次)", flush=True)
        try:
            with requests.get(url, stream=True, timeout=timeout, headers=headers) as resp:
                resp.raise_for_status()
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 15):
                        if chunk:
                            fh.write(chunk)
            part.replace(dest)
            print(f"[完成] {dest} ({dest.stat().st_size} bytes)", flush=True)
            return
        except Exception as exc:  # 网络类错误统一重试
            last_error = exc
            part.unlink(missing_ok=True)
            print(f"[重试] {url}: {exc}", file=sys.stderr, flush=True)
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"下载失败({retries} 次): {url}: {last_error}")


def convert_to_webp(src: Path, dst: Path, quality: int = 80,
                    max_size: int | None = None) -> bool:
    """把单张图片编码成 WebP。

    先编码到内存再落盘,这样源本身就是 .webp 时也不会在读文件的过程中覆盖自己。
    """
    try:
        with Image.open(src) as raw:
            raw.load()
            has_alpha = (
                raw.mode in ("RGBA", "LA")
                or (raw.mode == "P" and "transparency" in raw.info)
            )
            im = ImageOps.exif_transpose(raw)
            im = im.convert("RGBA" if has_alpha else "RGB")
            if max_size:
                im.thumbnail((max_size, max_size), Image.LANCZOS)
            buffer = io.BytesIO()
            im.save(buffer, "WEBP", quality=quality, method=6)
            data = buffer.getvalue()

        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return True
    except Exception as exc:
        print(f"[跳过] 无法转换 {src}: {exc}", file=sys.stderr, flush=True)
        return False


def process_zip(zip_path: Path, out_dir: Path, quality: int = 80,
                max_size: int | None = None) -> tuple[int, int]:
    """解压 zip,把里面的图片转成 WebP(保持目录结构),并删除已转换的原图。"""
    print(f"[解压] {zip_path}", flush=True)
    out_dir = out_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        # zip slip 防护:逐个成员确认解压后的绝对路径确实落在目标目录内
        for member in zf.infolist():
            target = (out_dir / member.filename).resolve()
            if not target.is_relative_to(out_dir):
                raise RuntimeError(f"非法路径(zip slip): {member.filename}")
        zf.extractall(out_dir)

    converted = skipped = 0
    for root, _, files in os.walk(out_dir):
        for name in files:
            src = Path(root) / name
            if src.suffix.lower() not in IMG_EXTS:
                continue
            dst = src.with_suffix(".webp")
            if convert_to_webp(src, dst, quality=quality, max_size=max_size):
                converted += 1
                if dst != src:
                    src.unlink(missing_ok=True)
            else:
                skipped += 1
    print(f"[处理完成] {zip_path}: 转换 {converted} 张, 跳过 {skipped} 张", flush=True)
    return converted, skipped


def rezip(src_dir: Path, zip_out: Path) -> None:
    """把目录重新打包成 zip(最高等级 deflate 压缩)。"""
    print(f"[打包] {src_dir} -> {zip_out}", flush=True)
    zip_out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, _, files in os.walk(src_dir):
            for name in sorted(files):
                full = Path(root) / name
                zf.write(full, full.relative_to(src_dir))
    print(f"[完成] {zip_out} ({zip_out.stat().st_size} bytes)", flush=True)


def read_urls(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [line.strip() for line in lines
            if line.strip() and not line.lstrip().startswith("#")]


def output_name(url: str, index: int, used: set[str]) -> str:
    """从 URL 推导输出文件名;同名时自动加序号,避免多个包互相覆盖。"""
    name = url.split("?")[0].rstrip("/").split("/")[-1] or f"archive_{index}.zip"
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-") or f"archive_{index}.zip"
    if not name.lower().endswith(".zip"):
        name = f"archive_{index}.zip"
    stem = name[:-4]
    candidate, seq = name, 2
    while candidate in used:
        candidate = f"{stem}_{seq}.zip"
        seq += 1
    used.add(candidate)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", required=True, help="URL 列表文件(每行一个)")
    parser.add_argument("--output", default="output", help="输出目录")
    parser.add_argument("--quality", type=int, default=80, help="WebP 质量 (1-100)")
    parser.add_argument("--max-size", type=int, default=None, help="最大边长(像素)")
    parser.add_argument("--keep-originals", action="store_true", help="保留原始下载的 zip")
    args = parser.parse_args()

    if not 1 <= args.quality <= 100:
        parser.error("--quality 必须在 1-100 之间")
    if args.max_size is not None and args.max_size < 1:
        parser.error("--max-size 必须为正整数")

    url_file = Path(args.urls)
    if not url_file.is_file():
        print(f"[错误] 找不到 URL 列表: {url_file}", file=sys.stderr)
        set_github_output("has_output", "false")
        return 1

    urls = read_urls(url_file)
    if not urls:
        print("没有 URL 可处理(urls.txt 为空或全是注释),直接跳过。")
        set_github_output("has_output", "false")
        return 0

    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)
    downloads = out_root / "_downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    produced = failed = 0

    for index, url in enumerate(urls, 1):
        name = output_name(url, index, used)
        zip_path = downloads / name
        try:
            download(url, zip_path)

            # 每个 zip 用独立临时目录,互不干扰,结束后自动清理
            with tempfile.TemporaryDirectory(prefix="webp-") as tmp:
                tmp_dir = Path(tmp)
                process_zip(zip_path, tmp_dir, quality=args.quality,
                            max_size=args.max_size)
                rezip(tmp_dir, out_root / name)

            produced += 1
            if not args.keep_originals:
                zip_path.unlink(missing_ok=True)
                print(f"[删除原包] {zip_path}", flush=True)
        except Exception as exc:
            failed += 1
            zip_path.unlink(missing_ok=True)
            print(f"[错误] 处理 {url} 失败: {exc}", file=sys.stderr, flush=True)
            continue

    # 清理空的下载目录
    if not args.keep_originals and downloads.exists() and not any(downloads.iterdir()):
        shutil.rmtree(downloads, ignore_errors=True)

    print(f"[全部完成] 成功 {produced} 个, 失败 {failed} 个 -> {out_root}", flush=True)
    set_github_output("has_output", "true" if produced else "false")
    set_github_output("produced", str(produced))
    set_github_output("failed", str(failed))
    return 0 if produced else 1


if __name__ == "__main__":
    sys.exit(main())
