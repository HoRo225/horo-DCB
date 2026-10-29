import json
import unittest

from src.steam.domain.offers import Announcement, SteamOffer, announced_app

OFFER = SteamOffer(7, "遊戲", "NT$100", "說明", "https://cdn.example.com/h.jpg", ends_at=1792083600)
PERSISTED = {
    "offer": {"app_id": 7, "name": "遊戲", "old_price": "NT$100", "description": "說明",
              "header_image": "https://cdn.example.com/h.jpg", "ends_at": 1792083600},
    "role_ids": [2, 1],
}


class AnnouncementTest(unittest.TestCase):
    def test_payload_keeps_the_persisted_shape(self) -> None:
        payload = json.loads(json.dumps(Announcement(OFFER, (2, 1)).payload()))
        self.assertEqual(payload, PERSISTED)
        self.assertEqual(announced_app(payload), 7)

    def test_saved_payload_reads_back_as_the_same_announcement(self) -> None:
        self.assertEqual(Announcement.parse(PERSISTED), Announcement(OFFER, (2, 1)))

    def test_store_url_points_at_the_app(self) -> None:
        self.assertEqual(OFFER.store_url, "https://store.steampowered.com/app/7/")


if __name__ == "__main__":
    unittest.main()
