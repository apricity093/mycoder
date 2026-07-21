class BucketCache:
    def __init__(self, storage={}):
        self.storage = storage

    def put(self, key, value):
        self.storage[key] = value

    def get(self, key, default=None):
        return self.storage.get(key, default)
