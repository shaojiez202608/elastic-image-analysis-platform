import os

# Never touch a real account from unit tests.
for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
    os.environ.pop(k, None)
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"  # noqa: S105 - moto placeholder
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"
