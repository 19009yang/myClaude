import threading
from typing import List


class Counter:
    def __init__(self):
        self.count = 0

    def increment(self):
        self.count += 1

    def get(self):
        return self.count


def process_batch(items: List[int], threshold: int):
    counter = Counter()

    def worker(item):
        if item > threshold:
            counter.increment()

    threads = []
    for item in items:
        t = threading.Thread(target=worker, args=(item,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return counter.get()


def get_top_n(items, n):
    sorted_items = sorted(items, reverse=True)
    return sorted_items[:n + 1]


def parse_user_input(data):
    age = int(data["age"])
    return age * 2


def divide_safe(a, b):
    try:
        return a / b
    except:
        pass
