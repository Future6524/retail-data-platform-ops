"""In-memory stand-in for the boto3 S3 client subset used by retail_ops. Models ETags, If-Match / If-None-Match
and ListObjectsV2 pagination. This is a MOCK: tests using it prove our logic, not that real S3 behaves identically."""
import hashlib
import io


class FakeClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, page_size: int = 1000):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.page_size = page_size
        self.calls: list[tuple[str, str]] = []
        self.before_put = None  # hook(key) -> None, lets a test simulate a concurrent writer

    @staticmethod
    def _etag(data: bytes) -> str:
        return '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'

    def seed(self, key: str, data: bytes) -> str:
        self.objects[key] = (data, self._etag(data))
        return self.objects[key][1]

    def get_object(self, Bucket, Key, **kw):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        data, etag = self.objects[Key]
        return {"Body": io.BytesIO(data), "ETag": etag}

    def put_object(self, Bucket, Key, Body, **kw):
        self.calls.append(("put", Key))
        if self.before_put:
            self.before_put(Key)
        data = Body if isinstance(Body, bytes) else Body.read()
        if kw.get("IfNoneMatch") == "*" and Key in self.objects:
            raise FakeClientError("PreconditionFailed")
        if "IfMatch" in kw and (Key not in self.objects or self.objects[Key][1] != kw["IfMatch"]):
            raise FakeClientError("PreconditionFailed")
        self.objects[Key] = (data, self._etag(data + str(len(self.calls)).encode()))
        return {"ETag": self.objects[Key][1]}

    def delete_object(self, Bucket, Key, **kw):
        self.calls.append(("delete", Key))
        if "IfMatch" in kw and (Key in self.objects and self.objects[Key][1] != kw["IfMatch"]):
            raise FakeClientError("PreconditionFailed")
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None, **kw):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        more = start + self.page_size < len(keys)
        out = {"Contents": [{"Key": k, "Size": len(self.objects[k][0])} for k in page], "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = str(start + self.page_size)
        return out
