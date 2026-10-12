"""
Common utilities for message handling across agent clients
"""
from typing import Dict, Any, List, Optional


class MessageUtils:
    """Utilities for extracting and formatting messages"""
    
    @staticmethod
    def get_last_user_message(messages: List[Dict[str, Any]]) -> Optional[str]:
        """
        Extract the last user/human message content from message list
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Content of the last user message, or None if not found
        """
        for msg in reversed(messages):
            if isinstance(msg, dict):
                msg_type = msg.get("type", "")
                if msg_type in ["human", "user"]:
                    return MessageUtils.extract_message_content(msg)
        return None
    
    @staticmethod
    def extract_message_content(message: Dict[str, Any]) -> str:
        """
        Extract text content from a message in various formats
        
        Args:
            message: Message dictionary with content field
            
        Returns:
            Extracted text content as string
        """
        content = message.get("content", "")
        
        # Handle string content
        if isinstance(content, str):
            return content
        
        # Handle list content (content blocks)
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if "text" in block:
                        texts.append(block["text"])
                    elif "content" in block:
                        texts.append(str(block["content"]))
                elif isinstance(block, str):
                    texts.append(block)
            return " ".join(texts)
        
        # Handle other types
        return str(content)
    
    @staticmethod
    def format_messages_for_bedrock(
        messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Format messages for Bedrock Converse API
        
        Converts internal message format to Bedrock API format:
        - Maps role types (human/user -> user, ai/assistant -> assistant)
        - Converts content to content blocks format
        - Filters out system messages (handled separately)
        
        Args:
            messages: List of messages in internal format
            
        Returns:
            List of messages formatted for Bedrock Converse API
        """
        formatted = []
        
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            
            # Bedrock Converse API requires "user" or "assistant" role
            msg_type = msg.get("type", "")
            if msg_type == "system":
                # System messages are handled separately in Converse API
                continue
            
            # Map role: human/user -> user, ai/assistant -> assistant
            role = "assistant" if msg_type in ["ai", "assistant"] else "user"
            
            # Convert content to content blocks format
            content = msg.get("content", "")
            content_blocks = MessageUtils._convert_to_content_blocks(content)
            
            # Skip messages with no content
            if content_blocks:
                formatted.append({"role": role, "content": content_blocks})
        
        return formatted
    
    @staticmethod
    def _convert_to_content_blocks(content: Any) -> List[Dict[str, str]]:
        """
        Convert various content formats to Bedrock content blocks
        
        Args:
            content: Content in various formats (str, list, dict)
            
        Returns:
            List of content blocks with text field
        """
        if isinstance(content, list):
            # Already in list format, ensure proper structure
            content_blocks = []
            for block in content:
                text_value = MessageUtils._extract_text_from_block(block)
                if text_value:
                    content_blocks.append({"text": text_value})
            return content_blocks
        
        elif isinstance(content, str) and content.strip():
            return [{"text": content}]
        
        elif content:
            str_content = str(content).strip()
            if str_content:
                return [{"text": str_content}]
        
        return []
    
    @staticmethod
    def _extract_text_from_block(block: Any) -> Optional[str]:
        """
        Extract text from a content block
        
        Args:
            block: Content block in various formats
            
        Returns:
            Extracted text or None if empty
        """
        if isinstance(block, dict):
            text_value = block.get("text", str(block)) if "text" in block else str(block)
            return text_value if text_value and str(text_value).strip() else None
        
        elif isinstance(block, str) and block.strip():
            return block
        
        elif block:
            str_value = str(block).strip()
            return str_value if str_value else None
        
        return None
    
    @staticmethod
    def build_strands_conversation(
        messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Build conversation messages for Strands Agent SDK
        
        Args:
            messages: List of messages in internal format
            
        Returns:
            List of messages formatted for Strands Agent
        """
        conversation_messages = []
        
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            
            msg_type = msg.get("type", "")
            if msg_type in ["human", "user", "ai", "assistant"]:
                role = "user" if msg_type in ["human", "user"] else "assistant"
                content = msg.get("content", "")
                
                # Format content as list of content blocks
                if isinstance(content, str):
                    content_blocks = [{"text": content}]
                elif isinstance(content, list):
                    # Extract text from content blocks
                    content_blocks = []
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            content_blocks.append({"text": block["text"]})
                        elif isinstance(block, str):
                            content_blocks.append({"text": block})
                else:
                    content_blocks = [{"text": str(content)}]
                
                if content_blocks:
                    conversation_messages.append({
                        "role": role,
                        "content": content_blocks
                    })
        
        return conversation_messages
