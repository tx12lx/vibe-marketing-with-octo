from __future__ import annotations

import io
import re
from typing import Optional

import pdfplumber
import requests
import urllib3

import google.auth
import google.auth.transport.requests

# Corporate SSL inspection proxies replace certificates with company-signed ones
# that Python's bundled CA store doesn't trust. Disable verification to match
# browser behaviour on the same network.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_GDOC_PATTERN = re.compile(r"docs\.google\.com/document/d/([^/?#]+)")
_GSHEET_PATTERN = re.compile(r"docs\.google\.com/spreadsheets/d/([^/?#]+)")
_GDRIVE_PATTERN = re.compile(r"drive\.google\.com/file/d/([^/?#]+)")


def _make_session() -> requests.Session:
    """Return a requests session authenticated with ADC credentials where available."""
    try:
        credentials, _ = google.auth.default()
        session = google.auth.transport.requests.AuthorizedSession(credentials)
    except Exception:
        session = requests.Session()
    session.headers.update({"User-Agent": "VibeBriefing/1.0"})
    session.verify = False
    return session


class BriefFetchError(Exception):
    pass


class BriefFetcher:
    def __init__(self, session: Optional[requests.Session] = None, timeout: int = 60):
        self.session = session or _make_session()
        self.timeout = timeout

    def fetch(self, url: str) -> str:
        url = url.strip()
        try:
            if m := _GDOC_PATTERN.search(url):
                return self._fetch_google_doc(m.group(1))
            if m := _GSHEET_PATTERN.search(url):
                return self._fetch_google_sheet(url, m.group(1))
            if m := _GDRIVE_PATTERN.search(url):
                return self._fetch_drive_file(m.group(1))
            if self._looks_like_pdf(url):
                return self._fetch_pdf(url)
            return self._fetch_generic(url)
        except BriefFetchError:
            raise
        except Exception as exc:
            raise BriefFetchError(f"Failed to fetch {url}: {exc}") from exc

    def _fetch_google_doc(self, doc_id: str) -> str:
        export_url = (
            f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        )
        resp = self.session.get(export_url, timeout=self.timeout)
        if resp.status_code == 403:
            raise BriefFetchError(
                f"Access denied for Google Doc {doc_id}. "
                "Document must be shared publicly or with the service account."
            )
        resp.raise_for_status()
        return resp.text.strip()

    def _fetch_google_sheet(self, url: str, sheet_id: str) -> str:
        gid = None
        if "#gid=" in url:
            gid = url.split("#gid=")[1].split("&")[0]
        export_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        )
        if gid:
            export_url += f"&gid={gid}"
        resp = self.session.get(export_url, timeout=self.timeout)
        if resp.status_code == 403:
            raise BriefFetchError(
                f"Access denied for Google Sheet {sheet_id}. "
                "Sheet must be shared publicly or with the service account."
            )
        resp.raise_for_status()
        return resp.text.strip()

    def _fetch_drive_file(self, file_id: str) -> str:
        # Try as plain text first; fall back to PDF extraction
        download_url = (
            f"https://drive.google.com/uc?export=download&id={file_id}"
        )
        resp = self.session.get(download_url, timeout=self.timeout, allow_redirects=True)
        if resp.status_code == 403:
            raise BriefFetchError(
                f"Access denied for Drive file {file_id}. "
                "File must be shared publicly or with the service account."
            )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type:
            return self._extract_pdf_text(resp.content)
        return resp.text.strip()

    def _fetch_pdf(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return self._extract_pdf_text(resp.content)

    def _fetch_generic(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type:
            return self._extract_pdf_text(resp.content)
        return resp.text.strip()

    def _extract_pdf_text(self, content: bytes) -> str:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n".join(pages).strip()

    @staticmethod
    def _looks_like_pdf(url: str) -> bool:
        lower = url.lower().split("?")[0]
        return lower.endswith(".pdf")
