import json
import os
import time
import logging
import urllib.request
import csv
import io
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

logger = logging.getLogger("forex_factory_news")

CACHE_FILE = "forex_factory_cache.json"

class ForexFactoryNewsSentinel:
    """
    Forex Factory Economic Calendar Sentinel & Macro Guard.
    Monitors high-impact macroeconomic events (FOMC, CPI, NFP, GDP, Fed Rate Decisions)
    that cause massive liquidation wicks, spread expansion, and volatility in crypto futures.
    
    Multi-tier data source:
    1. CSV feed (https://nfs.faireconomy.media/ff_calendar_thisweek.csv)
    2. JSON feed (https://nfs.faireconomy.media/ff_calendar_thisweek.json)
    3. Persistent local disk cache (cached for 2 hours to respect rate limits)
    """
    def __init__(self, cache_duration_sec: int = 7200):
        self.cache_duration_sec = cache_duration_sec
        self.last_fetch_time: float = 0.0
        self.cached_events: List[Dict[str, Any]] = []
        self._load_disk_cache()

    def _load_disk_cache(self):
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        self.cached_events = data
                        self.last_fetch_time = os.path.getmtime(CACHE_FILE)
        except Exception as e:
            logger.debug(f"Could not load disk cache: {e}")

    def _save_disk_cache(self):
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cached_events, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save disk cache: {e}")

    def fetch_events(self, force: bool = False) -> List[Dict[str, Any]]:
        """Fetches calendar events with 2-hour caching and multi-tier fallbacks."""
        now = time.time()
        if not force and self.cached_events and (now - self.last_fetch_time < self.cache_duration_sec):
            return self.cached_events

        # Tier 1: CSV feed
        events = self._fetch_csv()
        if events:
            self.cached_events = events
            self.last_fetch_time = now
            self._save_disk_cache()
            return self.cached_events

        # Tier 2: JSON feed
        events = self._fetch_json()
        if events:
            self.cached_events = events
            self.last_fetch_time = now
            self._save_disk_cache()
            return self.cached_events

        return self.cached_events

    def _fetch_csv(self) -> Optional[List[Dict[str, Any]]]:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.csv"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    content = resp.read().decode("utf-8")
                    reader = csv.DictReader(io.StringIO(content))
                    parsed = []
                    # Eastern Time is UTC-4 in summer/fall (EDT) or UTC-5 in winter (EST)
                    tz_edt = timezone(timedelta(hours=-4))
                    for row in reader:
                        title = row.get("Title", "").strip()
                        country = row.get("Country", "").strip().upper()
                        impact = row.get("Impact", "").strip().capitalize()
                        date_str = row.get("Date", "").strip()
                        time_str = row.get("Time", "").strip()

                        # Parse datetime
                        dt_iso = ""
                        try:
                            # e.g., '09-27-2026 11:50pm'
                            if date_str and time_str and time_str.lower() not in ["all day", "tentative"]:
                                dt = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
                                dt_utc = dt.replace(tzinfo=tz_edt).astimezone(timezone.utc)
                                dt_iso = dt_utc.isoformat()
                        except Exception as e:
                            logger.debug(f"News date parse skipped: {e}")

                        parsed.append({
                            "title": title,
                            "country": country,
                            "impact": impact,
                            "date": dt_iso or date_str,
                            "forecast": row.get("Forecast", "").strip(),
                            "previous": row.get("Previous", "").strip()
                        })
                    if parsed:
                        return parsed
        except Exception as e:
            logger.debug(f"CSV fetch notice: {e}")
        return None

    def _fetch_json(self) -> Optional[List[Dict[str, Any]]]:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    raw_data = resp.read().decode("utf-8")
                    data = json.loads(raw_data)
                    if isinstance(data, list) and data:
                        return data
        except Exception as e:
            logger.debug(f"JSON fetch notice: {e}")
        return None

    @staticmethod
    def _parse_event_datetime(date_str: str) -> Optional[datetime]:
        """Parses ISO date string with timezone into UTC datetime."""
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(date_str)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def get_high_impact_usd_events(self, hours_ahead: float = 48.0) -> List[Dict[str, Any]]:
        """Returns upcoming High Impact USD events within the specified time window."""
        events = self.fetch_events()
        now_utc = datetime.now(timezone.utc)
        filtered = []

        critical_keywords = ["cpi", "fomc", "nfp", "employment", "interest rate", "powell", "gdp", "pce", "fed", "retail sales"]

        for ev in events:
            country = str(ev.get("country", "")).strip().upper()
            impact = str(ev.get("impact", "")).strip().capitalize()
            title = str(ev.get("title", "")).strip()

            is_high_impact = (impact == "High")
            is_critical_keyword = any(kw in title.lower() for kw in critical_keywords)

            # Focus on USD macroeconomic announcements
            if country in ["USD", "ALL"] and (is_high_impact or (impact == "Medium" and is_critical_keyword)):
                ev_dt = self._parse_event_datetime(ev.get("date"))
                if not ev_dt:
                    continue

                diff_seconds = (ev_dt - now_utc).total_seconds()
                diff_hours = diff_seconds / 3600.0

                # Include events from 1 hour ago up to hours_ahead in future
                if -1.0 <= diff_hours <= hours_ahead:
                    filtered.append({
                        "title": title,
                        "country": country,
                        "impact": impact,
                        "datetime_utc": ev_dt.isoformat(),
                        "minutes_away": round(diff_seconds / 60.0, 1),
                        "forecast": ev.get("forecast", ""),
                        "previous": ev.get("previous", "")
                    })

        # Sort by closest time
        filtered.sort(key=lambda x: x["minutes_away"])
        return filtered

    def check_news_guard(self, pre_minutes: int = 30, post_minutes: Optional[int] = None) -> Dict[str, Any]:
        """
        Evaluates whether current time is in the 'Red News Danger Window'.
        - pre_minutes: Buffer before news release to pause new aggressive entries (default: 30 min).
        - post_minutes: Maximum window after release during which NEW entries stay blocked while
          volatility settles. Default is config.volatility_calm_minutes. Inside this window the
          caller ALSO requires measured volatility to be calm; the window is a hard ceiling so an
          unusually long volatility storm cannot block entries forever.
        """
        if post_minutes is None:
            try:
                from src.config import config as _cfg
                post_minutes = int(getattr(_cfg, "volatility_calm_minutes", 45))
            except Exception:
                post_minutes = 45
        upcoming = self.get_high_impact_usd_events(hours_ahead=12.0)

        for ev in upcoming:
            mins = ev["minutes_away"]
            # Active danger window: between -post_minutes and +pre_minutes
            if -post_minutes <= mins <= pre_minutes:
                ev_dt = self._parse_event_datetime(ev.get("datetime_utc"))
                resume_time_str = ""
                mins_until_resume = max(0.1, round(mins + post_minutes, 1))
                if ev_dt:
                    resume_dt = ev_dt + timedelta(minutes=post_minutes)
                    resume_time_str = resume_dt.astimezone().strftime("%H:%M")
                    mins_until_resume = max(0.1, round((resume_dt - datetime.now(timezone.utc)).total_seconds() / 60.0, 1))

                if mins > 0:
                    status = "HIGH_IMPACT_IMMINENT"
                    desc = f"⚠️ [RED NEWS CAUTION] '{ev['country']} - {ev['title']}' rilis dalam {mins:.0f} menit! Menahan order baru untuk proteksi modal."
                else:
                    status = "NEWS_RELEASE_VOLATILITY"
                    passed_mins = abs(mins)
                    desc = f"⚡ [RED NEWS AKTIF] '{ev['country']} - {ev['title']}' baru saja dirilis ({passed_mins:.0f} mnt lalu)! Menunggu volatilitas reda."

                return {
                    "is_danger": True,
                    "status": status,
                    "pause_entries": True,
                    "imminent_event": ev,
                    "caution_reason": desc,
                    "minutes_to_release": mins,
                    "minutes_since_release": max(0.0, -mins) if mins <= 0 else 0.0,
                    "is_post_release": mins <= 0,
                    "minutes_until_resume": mins_until_resume,
                    "resume_time_str": resume_time_str,
                    "upcoming_events": upcoming[:3]
                }

        next_event = upcoming[0] if upcoming and upcoming[0]["minutes_away"] > pre_minutes else None
        next_notice = f"Next High-Impact News: '{next_event['title']}' dalam {next_event['minutes_away']/60:.1f} jam" if next_event else "Aman: Tidak ada berita besar USD dalam 12 jam ke depan."

        return {
            "is_danger": False,
            "status": "NORMAL",
            "pause_entries": False,
            "imminent_event": None,
            "caution_reason": next_notice,
            "upcoming_events": upcoming[:3]
        }
