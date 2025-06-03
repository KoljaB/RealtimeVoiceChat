import logging
import time
from typing import Generator, Optional

logger = logging.getLogger(__name__)

class LLM:
    """
    大型语言模型接口类。
    
    这个类提供了与LLM交互的接口，用于生成文本响应。
    支持多种后端（如Ollama、OpenAI等）。
    """
    
    def __init__(
        self, 
        backend: Optional[str] = None, 
        model: Optional[str] = None, 
        system_prompt: Optional[str] = None, 
        no_think: bool = False
    ):
        """
        初始化LLM类
        
        Args:
            backend: LLM后端提供者（例如"ollama", "openai"）
            model: 使用的模型标识符
            system_prompt: 系统提示
            no_think: 是否移除思考标签
        """
        self.logger = logging.getLogger(__name__)
        self.logger.info("🧠 LLM模块初始化")
        
        self.backend = backend
        self.model = model
        self.system_prompt = system_prompt
        self.no_think = no_think
        
        # 初始化后端客户端
        self.client = None
        self._initialize_backend()
        
        self.logger.info(f"🧠 LLM配置完成: backend={backend}, model={model}, no_think={no_think}")
        
    def _initialize_backend(self) -> None:
        """初始化LLM后端客户端"""
        if self.backend == "ollama":
            try:
                import ollama
                self.client = ollama.Client()
                self.logger.info("🧠 Ollama客户端初始化成功")
            except ImportError:
                self.logger.error("🧠 Ollama库未安装，请安装ollama包")
                raise
        elif self.backend == "openai":
            try:
                import openai
                self.client = openai.OpenAI()
                self.logger.info("🧠 OpenAI客户端初始化成功")
            except ImportError:
                self.logger.error("🧠 OpenAI库未安装，请安装openai包")
                raise
        else:
            self.logger.warning(f"🧠 未知的后端: {self.backend}，将使用模拟模式")
            
    def generate(
        self, 
        text: Optional[str] = None, 
        history: Optional[list] = None, 
        use_system_prompt: bool = True, 
        **kwargs
    ) -> Generator[str, None, None]:
        """
        生成文本响应。
        
        Args:
            text: 输入文本
            history: 对话历史
            use_system_prompt: 是否使用系统提示
            **kwargs: 其他参数
            
        Yields:
            str: 生成的文本片段
        """
        self.logger.info(f"🧠 LLM生成请求: text={text[:50] if text else 'None'}...")
        
        if self.backend == "ollama" and self.client:
            yield from self._generate_ollama(text, history, use_system_prompt, **kwargs)
        elif self.backend == "openai" and self.client:
            yield from self._generate_openai(text, history, use_system_prompt, **kwargs)
        else:
            # 模拟模式
            yield from self._generate_mock(text, history, use_system_prompt, **kwargs)
            
    def _generate_ollama(
        self, 
        text: str, 
        history: Optional[list], 
        use_system_prompt: bool, 
        **kwargs
    ) -> Generator[str, None, None]:
        """使用Ollama生成文本"""
        try:
            messages = []
            
            # 添加系统提示
            if use_system_prompt and self.system_prompt:
                messages.append({
                    "role": "system",
                    "content": self.system_prompt
                })
            
            # 添加历史对话
            if history:
                messages.extend(history)
                
            # 添加当前用户输入
            if text:
                messages.append({
                    "role": "user", 
                    "content": text
                })
            
            # 调用Ollama API
            response = self.client.chat(
                model=self.model,
                messages=messages,
                stream=True,
                **kwargs
            )
            
            for chunk in response:
                if chunk.get('message', {}).get('content'):
                    content = chunk['message']['content']
                    if self.no_think:
                        # 移除思考标签
                        content = self._remove_think_tags(content)
                    yield content
                    
        except Exception as e:
            self.logger.error(f"🧠 Ollama生成出错: {e}")
            yield f"抱歉，生成回答时出现错误: {str(e)}"
            
    def _generate_openai(
        self, 
        text: str, 
        history: Optional[list], 
        use_system_prompt: bool, 
        **kwargs
    ) -> Generator[str, None, None]:
        """使用OpenAI生成文本"""
        try:
            messages = []
            
            # 添加系统提示
            if use_system_prompt and self.system_prompt:
                messages.append({
                    "role": "system",
                    "content": self.system_prompt
                })
            
            # 添加历史对话
            if history:
                messages.extend(history)
                
            # 添加当前用户输入
            if text:
                messages.append({
                    "role": "user", 
                    "content": text
                })
            
            # 调用OpenAI API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
                **kwargs
            )
            
            for chunk in response:
                if chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    if self.no_think:
                        # 移除思考标签
                        content = self._remove_think_tags(content)
                    yield content
                    
        except Exception as e:
            self.logger.error(f"🧠 OpenAI生成出错: {e}")
            yield f"抱歉，生成回答时出现错误: {str(e)}"
            
    def _generate_mock(
        self, 
        text: str, 
        history: Optional[list], 
        use_system_prompt: bool, 
        **kwargs
    ) -> Generator[str, None, None]:
        """模拟生成文本（用于测试）"""
        self.logger.info("🧠 使用模拟模式生成文本")
        
        # 模拟生成延迟
        time.sleep(0.5)
        
        # 生成一个简单的响应
        if text:
            response = f"这是对'{text[:20]}...'的回应。我是一个语音助手，可以帮助您解答问题。"
        else:
            response = "我是一个语音助手，可以帮助您解答问题。"
        
        # 记录完整响应
        self.logger.info(f"🧠 生成完整响应: {response}")
        
        # 逐字返回响应，模拟流式生成
        for char in response:
            yield char
            time.sleep(0.05)  # 模拟生成每个字符的延迟
            
    def _remove_think_tags(self, text: str) -> str:
        """移除思考标签"""
        # 简单的思考标签移除逻辑
        # 可以根据需要扩展更复杂的处理
        import re
        
        # 移除 <think>...</think> 标签
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        
        # 移除 [思考]...[/思考] 标签
        text = re.sub(r'\[思考\].*?\[/思考\]', '', text, flags=re.DOTALL)
        
        return text.strip()
        
    def prewarm(self) -> None:
        """
        预热LLM模型，确保第一次推理不会有额外延迟。
        """
        self.logger.info(f"🧠 预热LLM模型: {self.model}")
        
        try:
            # 进行一次轻量级的推理
            list(self.generate("Hello", use_system_prompt=False))
            self.logger.info("🧠 LLM模型预热完成")
        except Exception as e:
            self.logger.warning(f"🧠 LLM模型预热失败: {e}")
        
    def measure_inference_time(self) -> float:
        """
        测量LLM推理时间。
        
        Returns:
            float: 推理时间（毫秒）
        """
        self.logger.info(f"🧠 测量LLM推理时间: {self.model}")
        
        start_time = time.time()
        try:
            # 进行一次测试推理
            list(self.generate("Test inference time", use_system_prompt=False))
            inference_time = (time.time() - start_time) * 1000
            self.logger.info(f"🧠 LLM推理时间: {inference_time:.2f}ms")
            return inference_time
        except Exception as e:
            self.logger.error(f"🧠 测量推理时间失败: {e}")
            return 1000.0  # 返回默认值
        
    def cancel_generation(self) -> None:
        """
        取消当前正在进行的生成。
        """
        self.logger.info("🧠 取消LLM生成")
        # 在实际实现中，这里会中断正在进行的生成过程
        # 由于流式生成的特性，通常通过外部停止信号来实现