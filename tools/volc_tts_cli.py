"""豆包语音合成 2.0 命令行验证器（不依赖 MaiBot）。

用途：先把 AppID / Access Key / 音色 ID 三项跑通，再回头看插件。

    set VOLC_APP_ID=1234567890
    set VOLC_ACCESS_KEY=xxxxxxxx
    python volc_tts_cli.py --speaker zh_female_xiaohe_uranus_bigtts --out demo.mp3
    python volc_tts_cli.py --text "[开心] 你好，我是豆包语音合成二点零"

新版控制台只需要 API Key：

    set VOLC_API_KEY=xxxxxxxx
    python volc_tts_cli.py --out demo.mp3

退出码：0 成功，2 鉴权/业务错误，3 网络错误。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"


def build_headers(api_key: str, app_id: str, access_key: str, resource_id: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    if api_key:
        headers["X-Api-Key"] = api_key
    else:
        headers["X-Api-App-Id"] = app_id
        headers["X-Api-Access-Key"] = access_key
    return headers


async def synth(
    *,
    text: str,
    speaker: str,
    resource_id: str,
    api_key: str,
    app_id: str,
    access_key: str,
    fmt: str,
    sample_rate: int,
    timeout: float,
) -> bytes:
    payload = {
        "user": {"uid": "volc-tts-cli"},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {"format": fmt, "sample_rate": sample_rate},
        },
    }
    buffer = bytearray()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream(
            "POST",
            ENDPOINT,
            headers=build_headers(api_key, app_id, access_key, resource_id),
            json=payload,
        ) as resp:
            logid = resp.headers.get("X-Tt-Logid", "")
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", "replace")
                raise SystemExit(f"HTTP {resp.status_code} logid={logid}\n{raw}")
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = chunk.get("data")
                if isinstance(data, str) and data:
                    buffer.extend(base64.b64decode(data))
                code = chunk.get("code")
                if code in (None, 0, "0"):
                    continue
                if str(code) == "20000000":
                    break
                raise SystemExit(f"业务错误 [{code}] {chunk.get('message')} logid={logid}")
    if not buffer:
        raise SystemExit("没有拿到音频数据：检查 speaker 与 resource_id 是否匹配、该音色是否已开通")
    return bytes(buffer)


def main() -> int:
    parser = argparse.ArgumentParser(description="豆包语音合成 2.0 命令行验证器")
    parser.add_argument("--text", default="你好，我是豆包语音合成二点零。", help="要合成的文本")
    parser.add_argument("--speaker", default="zh_female_xiaohe_uranus_bigtts", help="音色 voice_type")
    parser.add_argument("--resource-id", default=os.getenv("VOLC_RESOURCE_ID", "seed-tts-2.0"))
    parser.add_argument("--format", default="mp3", choices=["mp3", "ogg_opus", "pcm"])
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--out", default="volc_tts_out.mp3", help="输出文件")
    args = parser.parse_args()

    api_key = os.getenv("VOLC_API_KEY", "")
    app_id = os.getenv("VOLC_APP_ID", "")
    access_key = os.getenv("VOLC_ACCESS_KEY", "")
    if not api_key and not (app_id and access_key):
        print("缺少鉴权：设置 VOLC_API_KEY，或同时设置 VOLC_APP_ID 与 VOLC_ACCESS_KEY", file=sys.stderr)
        return 2

    try:
        audio = asyncio.run(
            synth(
                text=args.text,
                speaker=args.speaker,
                resource_id=args.resource_id,
                api_key=api_key,
                app_id=app_id,
                access_key=access_key,
                fmt=args.format,
                sample_rate=args.sample_rate,
                timeout=args.timeout,
            )
        )
    except httpx.HTTPError as exc:
        print(f"网络错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    out = Path(args.out)
    out.write_bytes(audio)
    head = audio[:4].hex()
    print(f"OK {len(audio)} 字节 -> {out.resolve()}  头 4 字节={head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
