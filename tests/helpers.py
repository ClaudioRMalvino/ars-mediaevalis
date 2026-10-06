"""Fixtures shared by the test modules."""

import io

from PIL import Image


def artwork_fields(object_id: int = 1, **overrides) -> dict:
    """Returns the keyword arguments of a complete, realistic Artwork with optional overrides."""

    fields: dict = {
        "object_id": object_id,
        "title": "The Annunciation",
        "artist": "Robert Campin",
        "artist_bio": "Netherlandish, ca. 1375–1444",
        "date": "ca. 1427–32",
        "culture": "South Netherlandish",
        "place": "Tournai, Belgium",
        "medium": "Oil on oak",
        "dimensions": "25 3/8 x 46 3/8 in.",
        "department": "The Cloisters",
        "image_url": f"https://images.metmuseum.org/CRDImages/cl/original/{object_id}.jpg",
        "thumb_url": f"https://images.metmuseum.org/CRDImages/cl/web-large/{object_id}.jpg",
        "object_url": f"https://www.metmuseum.org/art/collection/search/{object_id}",
        "wikidata_url": "https://www.wikidata.org/wiki/Q123",
        "artist_wikidata_url": "https://www.wikidata.org/wiki/Q456",
        "credit": "The Cloisters Collection, 1956",
    }
    fields.update(overrides)
    return fields


def met_record(object_id: int = 1, **overrides) -> dict:
    """Returns a minimal but realistic Met /objects/{id} record with optional overrides."""

    obj: dict = {
        "objectID": object_id,
        "isPublicDomain": True,
        "primaryImage": f"https://images.metmuseum.org/CRDImages/cl/original/{object_id}.jpg",
        "primaryImageSmall": f"https://images.metmuseum.org/CRDImages/cl/web-large/{object_id}.jpg",
        "title": f"Panel {object_id}",
        "artistDisplayName": "Robert Campin",
        "objectDate": "ca. 1427–32",
        "country": "Belgium",
        "medium": "Oil on oak",
        "classification": "Paintings",
        "department": "The Cloisters",
        "objectURL": f"https://www.metmuseum.org/art/collection/search/{object_id}",
        "objectWikidata_URL": "",
        "artistWikidata_URL": "",
    }
    obj.update(overrides)
    return obj


def jpeg_bytes(size: tuple[int, int] = (64, 48)) -> bytes:
    """Returns the bytes of a small, valid JPEG."""

    buf: io.BytesIO = io.BytesIO()
    Image.new("RGB", size, (150, 40, 30)).save(buf, "JPEG")
    return buf.getvalue()
