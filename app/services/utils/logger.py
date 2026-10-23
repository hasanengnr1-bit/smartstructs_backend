import logging
import sys

# Define the log format
# %(name)s helps you identify which file/module the error came from
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

def get_logger(name: str):
    logger = logging.getLogger(name)
    
    # Only add handlers if they don't exist (prevents duplicate logs)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        # StreamHandler sends logs to the console (Standard Out)
        # This is what Docker and Cloud providers (AWS, GCP) capture
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
        
    return logger