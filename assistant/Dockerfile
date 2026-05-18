FROM python:3.11

WORKDIR /app

ARG TORCH_CUDA_INDEX=https://download.pytorch.org/whl/cu121
RUN pip install --no-cache-dir \
        --index-url ${TORCH_CUDA_INDEX} \
        --extra-index-url https://pypi.org/simple \
        "torch>=2.2"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]
