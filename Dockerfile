FROM python:3.12-slim
WORKDIR /app/whoosh
COPY . /app/whoosh/
ENV PYTHONPATH=/app/whoosh/src
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
