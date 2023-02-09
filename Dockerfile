FROM python:3.10

ADD src/main.py /
ADD requirements.txt /

RUN pip install -r requirements.txt

CMD ["python3", "main.py"]
