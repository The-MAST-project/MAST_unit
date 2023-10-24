#FROM ubuntu:latest
FROM python:3.10.4
LABEL authors="Arie Blumenzweig"

WORKDIR /mast/MAST_unit

COPY src              ./src
COPY static           ./static
COPY requirements.txt ./
COPY packages         ./packages

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r ./requirements.txt
# RUN pip install --no-cache-dir ./packages/pywin32-306-cp310-cp310-win_amd64.whl

CMD ["python", "src/app.py"]