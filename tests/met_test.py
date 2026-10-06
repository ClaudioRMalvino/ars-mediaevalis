import os
import time
import unittest

import httpx
from pathlib import Path
from src.ars_mediaevalis.museums import met
from src.ars_mediaevalis.paths import cache_dir

class TestMetFunctions(unittest.TestCase):
    """Tests that met functions operate appropriately."""

    def testCaching(self) -> None:
        """Tests that caching the objectID pools works properly."""

        file: Path = cache_dir() / "pool.json"
        if (file.exists()):
            os.remove(file)

        exec_time = []
        for i in range(2):
            start_time = time.perf_counter()

            client: httpx.Client = met.make_client()
            pool = met.load_pool(client)

            end_time = time.perf_counter()
            exec_time.append(end_time - start_time)

            print(len(pool))
            print(met.get_object(client, pool[0])["title"])

        print(f"Cached time = {exec_time[1]}, First run = {exec_time[0]}")
        self.assertLess(exec_time[1], exec_time[0])
