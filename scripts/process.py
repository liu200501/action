#!/usr/bin/env python3
import os
import sys
import shutil
import zipfile
import tempfile
import argparse
import multiprocessing as mp
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

import requests
from PIL import Image

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp"}
DEFAULT_DOWNLOAD_WORKERS = 4


def download(url: str, dest: Path, timeout=180) -> Path:
    print(f"[下载开始] {url}")
    headers = {"User-Agent": "Mozilla/5.0 (GitHubActions)"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    print(f"[下载完成] {dest.name} ({dest.stat().st_size} bytes)")
    return dest


def download_all(urls, downloads_dir: Path, workers: int = DEFAULT_DOWNLOAD_WORKERS):
    tasks = []
    for i, url in enumerate(urls, 1):
        name = url.split("?")[0].rstrip("/").split("/")[-1] or f"archive_{i}.zip"
        if not name.lower().endswith(".zip"):
            name = f"archive_{i}.zip"
        tasks.append((url, downloads_dir / name))

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(download, url, path): (url, path) for url, path in tasks
        }
        for fut in as_completed(future_map):
            url, path = future_map[fut]
            try:
                fut.result()
                results[url] = (path, None)
            except Exception as e:
                print(f"[下载失败] {url}: {e}", file=sys.stderr)
                results[url] = (None, e)
    return [(u, *results[u]) for u in urls]


def convert_one(args):
    src_str, dst_str, quality, max_size = args
    src, dst = Path(src_str), Path(dst_str)
    try:
        with Image.open(src) as im:
            im.load()
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
            else:
                im = im.convert("RGB")
            if max_size:
                im.thumbnail((max_size, max_size), Image.LANCZOS)
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, "WEBP", quality=quality, method=4)
        if dst != src:
            src.unlink(missing_ok=True)
        return (str(src), True, None)
    except Exception as e:
        return (str(src), False, str(e))


def convert_images_in_dir(root_dir: Path, quality: int, max_size, workers: int):
    tasks = []
    for r, _, files in os.walk(root_dir):
        for name in files:
            src = Path(r) / name
            if src.suffix.lower() in IMG_EXTS:
                tasks.append((str(src), str(src.with_suffix(".webp")), quality, max_size))
    if not tasks:
        print("  (该包内没有可转换的图片)")
        return
    print(f"  开始转换 {len(tasks)} 张图片,进程数={workers}")
    ok = fail = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for src, success, err in pool.map(convert_one, tasks, chunksize=8):
            if success:
                ok += 1
            else:
                fail += 1
                print(f"  [跳过] {src}: {err}", file=sys.stderr)
    print(f"  转换完成:成功 {ok},失败 {fail}")


def safe_extract(zip_path: Path, out_dir: Path):
    with zipfile.ZipFile(zip_path) as zf:
        out_resolved = out_dir.resolve()
        for member in zf.namelist():
            member_path = (out_dir / member).resolve()
            if not str(member_path).startswith(str(out_resolved)):
                raise RuntimeError(f"非法路径: {member}")
        zf.extractall(out_dir)


def rezip(src_dir: Path, zip_out: Path, compress_level: int):
    zip_out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=compress_level) as zf:
        for r, _, files in os.walk(src_dir):
            for name in files:
                full = Path(r) / name
                zf.write(full, full.relative_to(src_dir))


def process_one_zip(zip_path: Path, out_root: Path, quality, max_size,
                    convert_workers, compress_level):
    print(f"[处理] {zip_path.name}")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        safe_extract(zip_path, tmp_dir)
        convert_images_in_dir(tmp_dir, quality, max_size, convert_workers)
        out_zip = out_root / zip_path.name
        rezip(tmp_dir, out_zip, compress_level)
    print(f"[完成] {out_zip.name} ({out_zip.stat().st_size} bytes)")
    return out_zip


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls", required=True)
    parser.add_argument("--output", default="output")
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--max-size", type=int, default=None)
    parser.add_argument("--compress-level", type=int, default=6)
    parser.add_argument("--download-workers", type=int, default=DEFAULT_DOWNLOAD_WORKERS)
    parser.add_argument("--convert-workers", type=int,
                        default=max(1, (os.cpu_count() or 2)))
    parser.add_argument("--keep-originals", action="store_true")
    args = parser.parse_args()

    urls = [ln.strip() for ln in Path(args.urls).read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    if not urls:
        print("没有 URL 可处理")
        return

    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)
    downloads = out_root / "_downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    cpu = os.cpu_count() or 2
    print(f"CPU 核数={cpu},下载并发={args.download_workers},"
          f"转换进程={args.convert_workers},zip 级别={args.compress_level}")

    print(f"\n=== 阶段 1:并发下载 {len(urls)} 个压缩包 ===")
    results = download_all(urls, downloads, workers=args.download_workers)

    print(f"\n=== 阶段 2:解压 + 图片转 WebP + 重新打包 ===")
    for url, zip_path, err in results:
        if err or zip_path is None:
            print(f"[跳过] {url} 下载失败", file=sys.stderr)
            continue
        try:
            process_one_zip(
                zip_path, out_root,
                quality=args.quality,
                max_size=args.max_size,
                convert_workers=args.convert_workers,
                compress_level=args.compress_level,
            )
            if not args.keep_originals:
                zip_path.unlink(missing_ok=True)
                print(f"[删除原包] {zip_path.name}")
        except Exception as e:
            print(f"[错误] 处理 {zip_path.name} 失败: {e}", file=sys.stderr)

    if not args.keep_originals and downloads.exists() and not any(downloads.iterdir()):
        shutil.rmtree(downloads, ignore_errors=True)

    print("\n[全部完成]")


if __name__ == "__main__":
    mp.freeze_support()
    main()
