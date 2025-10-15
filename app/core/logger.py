import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime
import sys


class CustomLogger:
    """Custom logger for cron job application with file and console logging"""
    
    def __init__(self, name: str = "cronjob"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        
        # Prevent duplicate handlers
        if self.logger.handlers:
            return
            
        # Create logs directory if it doesn't exist
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # Create formatters
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        console_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # File handler with rotation (10MB per file, keep 5 backup files)
        log_file = os.path.join(log_dir, f"cronjob_{datetime.now().strftime('%Y%m%d')}.log")
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(console_formatter)
        
        # Error file handler - separate file for errors
        error_log_file = os.path.join(log_dir, f"error_{datetime.now().strftime('%Y%m%d')}.log")
        error_file_handler = RotatingFileHandler(
            error_log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        error_file_handler.setLevel(logging.ERROR)
        error_file_handler.setFormatter(file_formatter)
        
        # Add handlers to logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        self.logger.addHandler(error_file_handler)
    
    def debug(self, message: str):
        """Log debug message"""
        self.logger.debug(message)
    
    def info(self, message: str):
        """Log info message"""
        self.logger.info(message)
    
    def warning(self, message: str):
        """Log warning message"""
        self.logger.warning(message)
    
    def error(self, message: str, exc_info: bool = False):
        """Log error message"""
        self.logger.error(message, exc_info=exc_info)
    
    def critical(self, message: str, exc_info: bool = False):
        """Log critical message"""
        self.logger.critical(message, exc_info=exc_info)
    
    def success(self, message: str):
        """Log success message (using info level)"""
        self.logger.info(f"SUCCESS: {message}")
    
    def exception(self, message: str):
        """Log exception with traceback"""
        self.logger.exception(message)


# Create global logger instance
logger = CustomLogger()


# Convenience functions
def get_logger(name: str = "cronjob") -> CustomLogger:
    """Get a logger instance with a specific name"""
    return CustomLogger(name)


def log_job_start(job_name: str):
    """Log the start of a cron job"""
    logger.info(f"{'='*50}")
    logger.info(f"Starting job: {job_name}")
    logger.info(f"{'='*50}")


def log_job_end(job_name: str, success: bool = True):
    """Log the end of a cron job"""
    if success:
        logger.success(f"Job completed successfully: {job_name}")
    else:
        logger.error(f"Job failed: {job_name}")
    logger.info(f"{'='*50}\n")


def log_database_operation(operation: str, success: bool, details: str = ""):
    """Log database operations"""
    if success:
        logger.success(f"Database {operation}: {details}")
    else:
        logger.error(f"Database {operation} failed: {details}")


def log_api_call(endpoint: str, status_code: int, details: str = ""):
    """Log API calls"""
    if 200 <= status_code < 300:
        logger.info(f"API Call SUCCESS - {endpoint} - Status: {status_code} - {details}")
    elif 400 <= status_code < 500:
        logger.warning(f"API Call CLIENT ERROR - {endpoint} - Status: {status_code} - {details}")
    else:
        logger.error(f"API Call ERROR - {endpoint} - Status: {status_code} - {details}")

