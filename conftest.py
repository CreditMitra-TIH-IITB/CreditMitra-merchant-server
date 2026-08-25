import os

# The enrich endpoint fails closed when MERCHANT_API_KEY is unset, so the test
# key has to exist before app.main is imported by any test module.
os.environ.setdefault("MERCHANT_API_KEY", "test-key")
