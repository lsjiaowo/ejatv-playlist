#!/usr/bin/env python3
"""Crawl every public channel listed by eja.tv and export TXT/M3U."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
import sys
import time
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

BASE_URL = "http://eja.tv/"
USER_AGENT = "Mozilla/5.0 (compatible; ejatv-playlist-bot/1.0; +https://github.com/lsjiaowo/ejatv)"


@dataclass(frozen=True)
class Channel:
    name: str
    url: str
    country: str = ""
    country_code: str = ""
    languages: str = ""


@dataclass(frozen=True)
class Validation:
    channel: Channel
    playable: bool
    detail: str
    bytes_received: int = 0
    width: int | None = None
    height: int | None = None
    resolution_source: str = "unknown"


def clean_url(value: str) -> str:
    parts = urlsplit(html.unescape(value.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.channels: list[Channel] = []
        self.next_offset: int | None = None
        self._card = self._title = self._anchor = 0
        self._name: list[str] = []
        self._anchor_text: list[str] = []
        self._href = self._src = self._country = self._country_code = ""
        self._languages: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        if "card" in classes:
            self._card = 1
            self._name, self._languages = [], []
            self._src = self._country = self._country_code = ""
        elif self._card:
            self._card += 1
        if self._card and "card-title" in classes:
            self._title = 1
        elif self._title:
            self._title += 1
        if self._card and tag == "a":
            self._anchor = 1
            self._anchor_text = []
            self._href = attrs.get("href") or ""
        elif self._anchor:
            self._anchor += 1
        if self._card and tag == "source" and attrs.get("src"):
            self._src = clean_url(attrs["src"] or "")
        if tag == "a":
            match = re.search(r"(?:\?|&)offset=(\d+)", attrs.get("href") or "")
            if match:
                value = int(match.group(1))
                self.next_offset = max(self.next_offset or 0, value)

    def handle_data(self, data: str) -> None:
        if self._title:
            self._name.append(data)
        if self._anchor:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._anchor:
            self._anchor -= 1
            if self._anchor == 0 and tag == "a":
                text = " ".join("".join(self._anchor_text).split())
                country_match = re.search(r"[?&]country=([a-z]{2})", self._href, re.I)
                if country_match:
                    self._country_code = country_match.group(1).lower()
                    self._country = re.sub(r"^[^A-Za-z]+", "", text).strip()
                elif "language=" in self._href and text:
                    self._languages.append(text)
        if self._title:
            self._title -= 1
        if self._card:
            self._card -= 1
            if self._card == 0:
                name = " ".join("".join(self._name).split())
                if name and self._src.startswith(("http://", "https://")):
                    self.channels.append(Channel(name, self._src, self._country, self._country_code, ", ".join(self._languages)))


def fetch(url: str, timeout: float, retries: int) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(min(15, 2 ** attempt))
    raise RuntimeError(f"request failed after {retries} attempts: {url}: {last}")


def read_url(url: str, timeout: float, limit: int = 131072) -> tuple[bytes, str, int]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,video/*,*/*",
            "Range": f"bytes=0-{limit - 1}",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read(limit), response.headers.get("Content-Type", ""), response.status


def first_media_uri(manifest: str) -> str | None:
    for line in manifest.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def best_hls_variant(manifest: str) -> tuple[str, int, int] | None:
    """Return the URI and dimensions of the highest advertised HLS variant."""
    lines = [line.strip() for line in manifest.splitlines()]
    variants: list[tuple[str, int, int]] = []
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF:"):
            continue
        match = re.search(r"(?:^|,)RESOLUTION=(\d+)x(\d+)(?:,|$)", line, re.I)
        if not match:
            continue
        for uri in lines[index + 1:]:
            if uri and not uri.startswith("#"):
                variants.append((uri, int(match.group(1)), int(match.group(2))))
                break
    return max(variants, key=lambda item: (item[2], item[1])) if variants else None


def ffprobe_resolution(url: str, timeout: float) -> tuple[int, int] | None:
    """Read real video dimensions when the HLS manifest does not advertise them."""
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", url,
            ],
            capture_output=True, text=True, timeout=max(3.0, timeout), check=False,
        )
        streams = json.loads(completed.stdout or "{}").get("streams", [])
        sizes = [(int(s.get("width", 0)), int(s.get("height", 0))) for s in streams]
        sizes = [size for size in sizes if size[0] > 0 and size[1] > 0]
        return max(sizes, key=lambda item: (item[1], item[0])) if sizes else None
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


def validate_channel(channel: Channel, timeout: float) -> Validation:
    """Validate HLS media traffic and determine the highest video resolution."""
    width: int | None = None
    height: int | None = None
    resolution_source = "unknown"
    try:
        body, content_type, status = read_url(channel.url, timeout)
        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text.upper():
            return Validation(channel, False, f"HTTP {status}: not an HLS manifest", len(body))
        current_url = channel.url
        for _ in range(3):
            variant = best_hls_variant(text)
            if variant:
                uri, width, height = variant
                resolution_source = "hls-manifest"
            else:
                uri = first_media_uri(text)
            if not uri:
                return Validation(channel, False, "manifest has no media URI", len(body), width, height, resolution_source)
            target = urljoin(current_url, uri)
            target_body, target_type, target_status = read_url(target, timeout, 32768)
            target_text = target_body.decode("utf-8", "replace")
            if "#EXTM3U" in target_text.upper():
                body, content_type, status = target_body, target_type, target_status
                text = target_text
                current_url = target
                continue
            if target_status < 400 and len(target_body) >= 1024:
                if height is None:
                    probed = ffprobe_resolution(channel.url, timeout)
                    if probed:
                        width, height = probed
                        resolution_source = "ffprobe"
                return Validation(channel, True, f"HTTP {target_status} {target_type}; media bytes received", len(target_body), width, height, resolution_source)
            return Validation(channel, False, f"media response too small ({len(target_body)} bytes)", len(target_body), width, height, resolution_source)
        return Validation(channel, False, "nested playlist limit exceeded", len(body), width, height, resolution_source)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return Validation(channel, False, f"{type(exc).__name__}: {exc}", 0, width, height, resolution_source)


def crawl(args: argparse.Namespace) -> list[Channel]:
    result: list[Channel] = []
    seen_urls: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    offset = 0
    page = 0
    while page < args.max_pages:
        query = urlencode({
            "offset": offset,
            "country": args.country,
            "language": "",
            "category": "",
            "search": "",
        })
        parser = PageParser()
        parser.feed(fetch(f"{BASE_URL}?{query}", args.timeout, args.retries))
        signature = tuple(channel.url for channel in parser.channels)
        if not signature or signature in seen_pages:
            break
        seen_pages.add(signature)
        for channel in parser.channels:
            if channel.url not in seen_urls:
                seen_urls.add(channel.url)
                result.append(channel)
        page += 1
        print(f"page={page} offset={offset} found={len(parser.channels)} unique={len(result)}", flush=True)
        if parser.next_offset is None or parser.next_offset <= offset:
            break
        offset = parser.next_offset
        time.sleep(args.delay)
    else:
        if not args.allow_partial:
            raise RuntimeError(
                f"max page safety limit reached ({args.max_pages}); "
                "use --allow-partial for an intentional sample export"
            )
        print(f"sample_limit_reached={args.max_pages}", flush=True)
    return result


def attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;")


def write_outputs(
    channels: list[Channel],
    output: Path,
    validations: list[Validation] | None = None,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    channels = sorted(channels, key=channel_sort_key)
    (output / "ejatv.txt").write_text("".join(f"{c.name},{c.url}\n" for c in channels), encoding="utf-8")
    m3u = [f'#EXTM3U url-tvg=""\n# Generated: {generated}\n']
    for c in channels:
        group = c.country or "International"
        validation = next((item for item in validations or [] if item.channel.url == c.url), None)
        resolution = f'{validation.width}x{validation.height}' if validation and validation.width and validation.height else ''
        m3u.append(
            f'#EXTINF:-1 tvg-name="{attr(c.name)}" tvg-country="{attr(c.country_code.upper())}" '
            f'group-title="{attr(group)}" video-resolution="{resolution}",{c.name}\n{c.url}\n'
        )
    (output / "ejatv.m3u").write_text("".join(m3u), encoding="utf-8")
    metadata = {"generated_at": generated, "source": BASE_URL, "channels": len(channels)}
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "channels.json").write_text(json.dumps([asdict(c) for c in channels], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if validations is not None:
        report = [
            {
                **asdict(item.channel),
                "playable": item.playable,
                "detail": item.detail,
                "bytes_received": item.bytes_received,
                "width": item.width,
                "height": item.height,
                "resolution_source": item.resolution_source,
            }
            for item in validations
        ]
        (output / "validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        unknown = [entry for entry in report if entry["playable"] and entry["height"] is None]
        (output / "unknown-resolution.json").write_text(
            json.dumps(unknown, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output / "channels-all.json").write_text(
            json.dumps([asdict(item.channel) for item in validations], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


COUNTRY_LABELS = {"us": "美国", "jp": "日本", "uk": "英国"}


def channel_sort_key(channel: Channel) -> tuple[int, str, str]:
    """Sort US English channels first, then every section by channel name A-Z."""
    languages = {part.strip().casefold() for part in channel.languages.split(",") if part.strip()}
    language_rank = 0
    if channel.country_code.casefold() == "us":
        language_rank = 0 if "english" in languages else 1
    return language_rank, channel.name.casefold(), channel.url.casefold()


def write_combined_outputs(output: Path, countries: list[str]) -> None:
    """Combine validated country results into standard and genre-style playlists."""
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    standard = [f'#EXTM3U url-tvg=""\n# Generated: {generated}\n']
    genre: list[str] = []
    total = 0
    for code in countries:
        source = output / code / "channels.json"
        if not source.exists():
            raise RuntimeError(f"missing validated country output: {source}")
        channels = sorted(
            (Channel(**item) for item in json.loads(source.read_text(encoding="utf-8"))),
            key=channel_sort_key,
        )
        label = COUNTRY_LABELS.get(code, code.upper())
        genre.append(f"{label},#genre#\n")
        for channel in channels:
            standard.append(
                f'#EXTINF:-1 tvg-name="{attr(channel.name)}" tvg-country="{code.upper()}" '
                f'group-title="{label}",{channel.name}\n{channel.url}\n'
            )
            genre.append(f"{channel.name},{channel.url}\n")
            total += 1
    (output / "ejatv.m3u").write_text("".join(standard), encoding="utf-8")
    # Some Chinese IPTV clients use this #genre# convention even though it is
    # not part of the Extended M3U specification.
    (output / "ejatv_genre.m3u").write_text("".join(genre), encoding="utf-8")
    (output / "combined-metadata.json").write_text(
        json.dumps({"generated_at": generated, "countries": countries, "channels": total}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("playlist"))
    parser.add_argument("--country", default="", help="ISO two-letter country code, e.g. jp")
    parser.add_argument("--combine", action="store_true", help="combine existing validated country outputs")
    parser.add_argument("--countries", default="us,jp,uk", help="country order used by --combine")
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=1500)
    parser.add_argument("--validate", action="store_true", help="read an HLS media segment for every channel")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--probe-timeout", type=float, default=12)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="export when max-pages is reached; intended for small test runs",
    )
    args = parser.parse_args()
    if args.combine:
        countries = [item.strip().lower() for item in args.countries.split(",") if item.strip()]
        if not countries or any(not re.fullmatch(r"[a-z]{2}", item) for item in countries):
            parser.error("--countries must be comma-separated two-letter country codes")
        try:
            write_combined_outputs(args.output, countries)
            print(f"combined={','.join(countries)} output={args.output}")
            return 0
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.country and not re.fullmatch(r"[a-zA-Z]{2}", args.country):
        parser.error("--country must be an ISO two-letter code")
    args.country = args.country.lower()
    try:
        channels = crawl(args)
        if not channels:
            raise RuntimeError("no channels found")
        validations: list[Validation] | None = None
        if args.validate:
            order = {channel.url: index for index, channel in enumerate(channels)}
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                validations = list(pool.map(lambda c: validate_channel(c, args.probe_timeout), channels))
            validations.sort(key=lambda item: order[item.channel.url])
            playable = [
                item.channel for item in validations
                if item.playable and item.height is not None and item.height >= 720
            ]
            for item in validations:
                print(
                    f"probe={'ok' if item.playable else 'fail'} name={item.channel.name!r} "
                    f"bytes={item.bytes_received} resolution={item.width}x{item.height} "
                    f"source={item.resolution_source} detail={item.detail}",
                    flush=True,
                )
            channels = playable
        write_outputs(channels, args.output, validations)
        print(f"exported={len(channels)} output={args.output}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
