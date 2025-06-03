import asyncio
import logging
import os
import threading
import time
from queue import Queue, Empty
from typing import Callable, Generator, Optional, Tuple

import numpy as np
import torch
from chatterbox.tts import ChatterboxTTS, StreamingMetrics

logger = logging.getLogger(__name__)

# Configuration constants
DEFAULT_CHUNK_SIZE = 25  # Number of speech tokens per chunk
DEFAULT_TEMPERATURE = 0.8
DEFAULT_CFG_WEIGHT = 0.5
DEFAULT_EXAGGERATION = 0.5

class ChatterboxAudioProcessor:
    """
    原生Chatterbox音频处理器
    直接使用chatterbox-streaming，无适配层
    
    这个类提供了完整的TTS功能，包括：
    - 文本到语音的流式合成
    - 音频块的异步处理
    - 生成状态管理
    - 资源清理
    """
    
    def __init__(
        self, 
        device: str = "cuda",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        temperature: float = DEFAULT_TEMPERATURE,
        cfg_weight: float = DEFAULT_CFG_WEIGHT,
        exaggeration: float = DEFAULT_EXAGGERATION
    ):
        """
        初始化ChatterboxAudioProcessor
        
        Args:
            device: 计算设备 ("cuda", "cpu", "mps")
            chunk_size: 每个音频块的token数量
            temperature: 生成温度，控制随机性
            cfg_weight: 分类器自由引导权重
            exaggeration: 情感夸张程度
        """
        self.device = device
        self.chunk_size = chunk_size
        self.temperature = temperature
        self.cfg_weight = cfg_weight
        self.exaggeration = exaggeration
        
        # 初始化模型
        logger.info(f"🎵 初始化Chatterbox模型，设备: {device}")
        if device == "cuda" and not torch.cuda.is_available():
            logger.warning("CUDA不可用，切换到CPU")
            self.device = "cpu"
            
        self.model = ChatterboxTTS.from_pretrained(device=self.device)
        self.sr = self.model.sr  # 采样率
        
        # 状态管理
        self.is_generating = False
        self.stop_event = threading.Event()
        self.generation_lock = threading.Lock()
        
        # 回调函数
        self.on_first_audio_chunk_callback: Optional[Callable[[], None]] = None
        
        logger.info(f"🎵 Chatterbox模型初始化完成，采样率: {self.sr}Hz")
        
    def set_voice_from_audio(
        self, 
        audio_path: str, 
        exaggeration: Optional[float] = None
    ) -> None:
        """
        从音频文件设置语音风格
        
        Args:
            audio_path: 参考音频文件路径
            exaggeration: 情感夸张程度，如果为None则使用默认值
        """
        if exaggeration is None:
            exaggeration = self.exaggeration
            
        logger.info(f"🎵 设置语音风格，参考音频: {audio_path}")
        self.model.prepare_conditionals(audio_path, exaggeration=exaggeration)
        logger.info("🎵 语音风格设置完成")
        
    async def synthesize_text(
        self,
        text: str,
        audio_chunks: Queue,
        stop_event: threading.Event,
        generation_string: str = "",
        audio_prompt_path: Optional[str] = None,
        exaggeration: Optional[float] = None,
        cfg_weight: Optional[float] = None,
        temperature: Optional[float] = None,
        chunk_size: Optional[int] = None
    ) -> bool:
        """
        合成单个文本为音频流
        
        Args:
            text: 要合成的文本
            audio_chunks: 音频块输出队列
            stop_event: 停止事件
            generation_string: 生成标识字符串（用于日志）
            audio_prompt_path: 可选的音频提示路径
            exaggeration: 情感夸张程度
            cfg_weight: 分类器自由引导权重
            temperature: 生成温度
            chunk_size: 块大小
            
        Returns:
            bool: 如果合成完成返回True，如果被中断返回False
        """
        # 使用提供的参数或默认值
        exaggeration = exaggeration or self.exaggeration
        cfg_weight = cfg_weight or self.cfg_weight
        temperature = temperature or self.temperature
        chunk_size = chunk_size or self.chunk_size
        
        logger.info(f"🎵 {generation_string} 开始文本合成: {text[:50]}...")
        
        with self.generation_lock:
            if self.is_generating:
                logger.warning(f"🎵 {generation_string} 已有生成任务在进行，跳过")
                return False
            self.is_generating = True
            
        try:
            first_chunk_sent = False
            chunk_count = 0
            
            # 在线程池中运行同步的生成方法
            loop = asyncio.get_event_loop()
            
            def generate_audio():
                """在线程中运行的生成函数"""
                try:
                    for audio_chunk, metrics in self.model.generate_stream(
                        text=text,
                        audio_prompt_path=audio_prompt_path,
                        exaggeration=exaggeration,
                        cfg_weight=cfg_weight,
                        temperature=temperature,
                        chunk_size=chunk_size,
                        print_metrics=False
                    ):
                        if stop_event.is_set():
                            logger.info(f"🎵 {generation_string} 生成被中断")
                            return False
                            
                        # 转换为numpy数组
                        audio_data = audio_chunk.cpu().numpy().squeeze()
                        
                        # 确保音频数据在[-1, 1]范围内
                        if audio_data.max() > 1.0 or audio_data.min() < -1.0:
                            audio_data = np.clip(audio_data, -1.0, 1.0)
                        
                        # 转换为字节格式
                        audio_bytes = audio_data.tobytes()
                        
                        # 放入队列
                        try:
                            audio_chunks.put_nowait(audio_bytes)
                            chunk_count += 1
                            
                            # 触发第一个音频块回调
                            if not first_chunk_sent and self.on_first_audio_chunk_callback:
                                logger.info(f"🎵 {generation_string} 触发第一个音频块回调")
                                self.on_first_audio_chunk_callback()
                                first_chunk_sent = True
                                
                            logger.debug(f"🎵 {generation_string} 音频块已放入队列，大小: {len(audio_bytes)} 字节")
                            
                        except Exception as e:
                            logger.warning(f"🎵 {generation_string} 音频队列已满，丢弃块: {e}")
                            
                    logger.info(f"🎵 {generation_string} 文本合成完成，共生成{chunk_count}个音频块")
                    return True
                    
                except Exception as e:
                    logger.error(f"🎵 {generation_string} 生成过程中出错: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    return False
            
            # 在线程池中执行生成
            result = await loop.run_in_executor(None, generate_audio)
            return result
            
        finally:
            self.is_generating = False
            
    async def synthesize_generator(
        self,
        text_generator: Generator[str, None, None],
        audio_chunks: Queue,
        stop_event: threading.Event,
        generation_string: str = "",
        audio_prompt_path: Optional[str] = None,
        exaggeration: Optional[float] = None,
        cfg_weight: Optional[float] = None,
        temperature: Optional[float] = None,
        chunk_size: Optional[int] = None
    ) -> bool:
        """
        从文本生成器合成音频流
        
        Args:
            text_generator: 文本生成器
            audio_chunks: 音频块输出队列
            stop_event: 停止事件
            generation_string: 生成标识字符串（用于日志）
            audio_prompt_path: 可选的音频提示路径
            exaggeration: 情感夸张程度
            cfg_weight: 分类器自由引导权重
            temperature: 生成温度
            chunk_size: 块大小
            
        Returns:
            bool: 如果合成完成返回True，如果被中断返回False
        """
        logger.info(f"🎵 {generation_string} 开始生成器合成")
        
        # 收集所有文本
        accumulated_text = ""
        try:
            for text_chunk in text_generator:
                if stop_event.is_set():
                    logger.info(f"🎵 {generation_string} 生成器合成被中断")
                    return False
                accumulated_text += text_chunk
                
            # 如果有累积的文本，进行合成
            if accumulated_text.strip():
                return await self.synthesize_text(
                    text=accumulated_text,
                    audio_chunks=audio_chunks,
                    stop_event=stop_event,
                    generation_string=generation_string,
                    audio_prompt_path=audio_prompt_path,
                    exaggeration=exaggeration,
                    cfg_weight=cfg_weight,
                    temperature=temperature,
                    chunk_size=chunk_size
                )
            else:
                logger.warning(f"🎵 {generation_string} 生成器没有产生文本")
                return True
                
        except Exception as e:
            logger.error(f"🎵 {generation_string} 生成器合成出错: {e}")
            return False
            
    def stop_synthesis(self) -> None:
        """停止当前的合成任务"""
        logger.info("🎵 停止音频合成")
        self.stop_event.set()
        
    def is_synthesizing(self) -> bool:
        """检查是否正在合成"""
        return self.is_generating
        
    def cleanup_resources(self) -> None:
        """清理资源，特别是GPU内存"""
        logger.info("🎵 清理Chatterbox资源")
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.info("🎵 已清理CUDA缓存")
            
    def get_stream_info(self) -> Tuple[str, int, int]:
        """
        返回音频流的格式、通道数和采样率信息
        
        Returns:
            tuple: (format, channels, rate) 音频格式、通道数和采样率
        """
        return "pcm", 1, self.sr


# 为了向后兼容，保留AudioProcessor类名
class AudioProcessor(ChatterboxAudioProcessor):
    """
    向后兼容的AudioProcessor类
    现在直接继承自ChatterboxAudioProcessor
    """
    
    def __init__(self, engine: str = "chatterbox", **kwargs):
        """
        初始化AudioProcessor
        
        Args:
            engine: TTS引擎名称（现在只支持"chatterbox"）
            **kwargs: 其他参数传递给ChatterboxAudioProcessor
        """
        if engine != "chatterbox":
            logger.warning(f"不支持的引擎: {engine}，使用chatterbox")
            
        super().__init__(**kwargs)
        self.engine_name = "chatterbox"
        
        # 为了兼容性，添加一些旧的属性
        self.audio_chunks = asyncio.Queue()
        self.finished_event = threading.Event()
        
        # 设置默认语音（如果存在参考音频）
        reference_audio_path = "reference_audio.wav"
        if os.path.exists(reference_audio_path):
            try:
                self.set_voice_from_audio(reference_audio_path)
                logger.info(f"🎵 使用参考音频设置默认语音: {reference_audio_path}")
            except Exception as e:
                logger.warning(f"🎵 无法加载参考音频: {e}")
                
    def on_audio_stream_stop(self) -> None:
        """
        兼容性方法：音频流停止回调
        """
        logger.info("🎵 音频流停止")
        self.finished_event.set()
        
    def synthesize(
        self,
        text: str,
        audio_chunks: Queue,
        stop_event: threading.Event,
        generation_string: str = "",
    ) -> bool:
        """
        兼容性方法：同步合成文本
        
        Args:
            text: 要合成的文本
            audio_chunks: 音频块输出队列
            stop_event: 停止事件
            generation_string: 生成标识字符串
            
        Returns:
            bool: 合成是否成功完成
        """
        # 创建事件循环来运行异步方法
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self.synthesize_text(text, audio_chunks, stop_event, generation_string)
            )
            return result
        except Exception as e:
            logger.error(f"🎵 同步合成出错: {e}")
            return False
        finally:
            loop.close()
            
    def synthesize_generator(
        self,
        generator: Generator[str, None, None],
        audio_chunks: Queue,
        stop_event: threading.Event,
        generation_string: str = "",
    ) -> bool:
        """
        兼容性方法：同步合成生成器
        
        Args:
            generator: 文本生成器
            audio_chunks: 音频块输出队列
            stop_event: 停止事件
            generation_string: 生成标识字符串
            
        Returns:
            bool: 合成是否成功完成
        """
        # 创建事件循环来运行异步方法
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                super().synthesize_generator(generator, audio_chunks, stop_event, generation_string)
            )
            return result
        except Exception as e:
            logger.error(f"🎵 同步生成器合成出错: {e}")
            return False
        finally:
            loop.close()