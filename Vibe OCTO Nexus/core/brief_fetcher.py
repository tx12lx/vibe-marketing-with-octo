from __future__ import annotations

import io
import logging
import re
from typing import Optional

import pdfplumber
import requests
import urllib3

import google.auth
import google.auth.transport.requests

_log = logging.getLogger(__name__)

# Corporate SSL inspection proxies replace certificates with company-signed ones
# that Python's bundled CA store doesn't trust. Disable verification to match
# browser behaviour on the same network.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_GDOC_PATTERN = re.compile(r"docs\.google\.com/document/d/([^/?#]+)")
_GSHEET_PATTERN = re.compile(r"docs\.google\.com/spreadsheets/d/([^/?#]+)")
_GDRIVE_PATTERN = re.compile(r"drive\.google\.com/file/d/([^/?#]+)")


def _normalise_whitespace(text: str) -> str:
    """Collapse runs of 3+ newlines to 2 and strip leading/trailing space."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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

    def to_flat_string(self, url: str) -> str:
        """Fetch the document at url and return a single clean string.

        Google Sheets (multi-tab): iterates over all sheet tabs via the
          Sheets API v4 metadata endpoint, fetches each as CSV via the
          export URL (?format=csv&gid=...), joins them with
          '\\n\\n--- TAB: {tab_name} ---\\n\\n' separators, strips blank
          lines from each CSV block.
        Google Docs / plain text: returns content after whitespace normalisation.
        PDFs: delegates to _extract_pdf_text().
        On any fetch error: logs the error and returns "" so the ingestion
          pipeline continues with an empty brief text rather than aborting.
        """
        url = url.strip()
        if not url:
            return ""
        try:
            if m := _GSHEET_PATTERN.search(url):
                return self._fetch_sheet_all_tabs(m.group(1))
            if m := _GDOC_PATTERN.search(url):
                return _normalise_whitespace(self._fetch_google_doc(m.group(1)))
            if m := _GDRIVE_PATTERN.search(url):
                return _normalise_whitespace(self._fetch_drive_file(m.group(1)))
            if self._looks_like_pdf(url):
                return self._fetch_pdf(url)
            return _normalise_whitespace(self._fetch_generic(url))
        except Exception as exc:
            _log.warning("to_flat_string failed for %s: %s", url, exc)
            return ""

    def _fetch_sheet_all_tabs(self, sheet_id: str) -> str:
        """Fetch every tab of a Google Sheet and join with TAB-labelled separators."""
        meta_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
            "?fields=sheets.properties"
        )
        meta_resp = self.session.get(meta_url, timeout=self.timeout)
        if meta_resp.status_code in (401, 403, 404):
            raise BriefFetchError(
                f"Cannot access Sheet metadata for {sheet_id}: "
                f"HTTP {meta_resp.status_code}"
            )
        meta_resp.raise_for_status()

        sheets_data = meta_resp.json().get("sheets", [])
        blocks: list[str] = []

        for sheet_info in sheets_data:
            props = sheet_info.get("properties", {})
            tab_name = props.get("title", "Sheet")
            gid = props.get("sheetId", 0)

            export_url = (
                f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                f"/export?format=csv&gid={gid}"
            )
            csv_resp = self.session.get(export_url, timeout=self.timeout)
            if csv_resp.status_code != 200:
                continue  # skip inaccessible tabs; do not abort

            lines = [ln for ln in csv_resp.text.splitlines() if ln.strip()]
            if not lines:
                continue
            blocks.append(f"--- TAB: {tab_name} ---\n\n" + "\n".join(lines))

        return "\n\n".join(blocks)

    @staticmethod
    def _looks_like_pdf(url: str) -> bool:
        lower = url.lower().split("?")[0]
        return lower.endswith(".pdf")
