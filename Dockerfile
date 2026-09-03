FROM apify/actor-python-playwright:3.11

# Install Python requirements
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install browser build used by Scrapling
RUN scrapling install

# Copy application files
COPY . ./

# Environment configuration
ENV PYTHONUNBUFFERED=1

# Run the actor
CMD ["python3", "-m", "src.main"]
