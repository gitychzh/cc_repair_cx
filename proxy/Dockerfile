FROM litellm/litellm:v1.85.1
WORKDIR /app
RUN mkdir -p /app/logs
COPY proxy.py .
ENTRYPOINT ["python3"]
CMD ["proxy.py"]
