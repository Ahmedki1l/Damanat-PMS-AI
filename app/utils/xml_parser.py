# app/utils/xml_parser.py
"""
Helpers for parsing Hikvision ISAPI XML event payloads.
All Phase 1 cameras send EventNotificationAlert v2.0 XML.
"""

import xml.etree.ElementTree as ET
from typing import Optional

NS = "http://www.isapi.org/ver20/XMLSchema"


def find_text(root: ET.Element, tag: str) -> Optional[str]:
    """Find a tag with or without the ISAPI namespace and return its text."""
    el = root.find(f"{{{NS}}}{tag}")
    if el is None:
        el = root.find(tag)
    return el.text.strip() if el is not None and el.text else None


def find_text_in(parent: ET.Element, tag: str) -> Optional[str]:
    """Same as find_text but searches within a given parent element."""
    el = parent.find(f"{{{NS}}}{tag}")
    if el is None:
        el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else None


def parse_detection_region(root: ET.Element) -> tuple[Optional[str], Optional[str]]:
    """
    Extract (region_id, detection_target) from DetectionRegionList.
    Returns the first region entry found.
    """
    region_list = (
        root.find(f"{{{NS}}}DetectionRegionList") or
        root.find("DetectionRegionList")
    )
    if region_list is None:
        return None, None

    entry = (
        region_list.find(f"{{{NS}}}DetectionRegionEntry") or
        region_list.find("DetectionRegionEntry")
    )
    if entry is None:
        return None, None

    region_id = find_text_in(entry, "regionID")
    detection_target = find_text_in(entry, "detectionTarget")
    return region_id, detection_target


def safe_parse_xml(raw_body: bytes) -> Optional[ET.Element]:
    """Parse XML bytes safely. Returns None on parse error."""
    try:
        return ET.fromstring(raw_body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return None
