import logging
import sys


class LoggerSetup:
    """Centralized logging configuration"""
    
    @staticmethod
    def setup_logging():
        """Configure logging for the application with CloudWatch compatibility"""
        # Get root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        
        # Remove existing handlers to avoid duplicates
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # Create console handler that writes to stdout (captured by CloudWatch)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        
        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(formatter)
        
        # Add handler to root logger
        root_logger.addHandler(console_handler)
        
        # Set logging level for specific libraries
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('mcp').setLevel(logging.INFO)
        logging.getLogger('strands').setLevel(logging.INFO)
        
        # Log initialization
        logger = logging.getLogger(__name__)
        logger.info("Logging system initialized for AgentCore Runtime")
        
        return logger
