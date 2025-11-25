import asyncio
import base64
import io
import json

import httpx
from PIL import Image

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_onelastimage", "timetetng", "One Last Kiss 卢浮宫风格图片生成器", "1.0", "https://github.com/timetetng/astrbot_plugin_onelastimage")
class OneLastImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        """
        初始化插件，加载配置和 httpx 客户端。
        """
        super().__init__(context)
        self.config = config
        self.api_url = self.config.get("api_url")
        self.max_images = self.config.get("max_images", 5)
        self.max_file_size_mb = self.config.get("max_file_size_mb", 3)
        self.max_file_size_bytes = self.max_file_size_mb * 1024 * 1024 # 转换为字节

        default_params_config = self.config.get("default_params")

        if isinstance(default_params_config, dict):
            self.default_params = default_params_config
        elif isinstance(default_params_config, str):
            try:
                self.default_params = json.loads(default_params_config)
            except json.JSONDecodeError as e:
                logger.error(f"OneLastImage 插件 'default_params' 字符串配置解析失败: {e}. (配置内容: {default_params_config})。将使用空配置。")
                self.default_params = {}
        else:
            logger.warning(f"OneLastImage 插件 'default_params' 配置类型未知 (期待 dict 或 str, 得到 {type(default_params_config)})。将使用空配置。")
            self.default_params = {}

        self.client = httpx.AsyncClient()

        if not self.api_url or "YOUR_VERCEL_API_URL_HERE" in self.api_url:
            logger.error("OneLastImage 插件未配置 API URL！请在后台配置。")
            self.api_url = None # 禁用插件

    async def terminate(self):
        """
        插件终止时关闭 httpx 客户端。
        """
        await self.client.aclose()
        logger.info("OneLastImage 插件已关闭 httpx 客户端。")

    # 图片提取函数
    async def get_image_from_direct_event(self, event: AstrMessageEvent) -> list[Comp.Image]:
        """
        从当前事件中提取所有图片 (包括直接发送和回复的)
        """
        images = []
        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for component in event.message_obj.message:
                if isinstance(component, Comp.Image):
                    images.append(component)
                elif isinstance(component, Comp.Reply):
                    replied_chain = getattr(component, "chain", None)
                    if replied_chain:
                        for reply_comp in replied_chain:
                            if isinstance(reply_comp, Comp.Image):
                                images.append(reply_comp)
        unique_images = []
        seen = set()
        for img in images:
            identifier = img.url or img.file
            if identifier and identifier not in seen:
                unique_images.append(img); seen.add(identifier)
            elif not identifier:
                unique_images.append(img)
        return unique_images

    async def download_image(self, image: Comp.Image) -> bytes | None:
        """
        将 AstrBot 的图片组件 (Comp.Image) 下载为字节数据。
        """
        try:
            base64_str = await image.convert_to_base64()

            if not base64_str:
                logger.warning(f"convert_to_base64 failed for image: url={image.url}, file={image.file}")
                return None
            # 2. 将 Base64 字符串解码为原始字节
            return base64.b64decode(base64_str)

        except Exception as e:
            logger.error(f"Failed to download/read image using convert_to_base64 ({image.url or image.file}): {e}", exc_info=True)
            return None

    async def process_and_compress_image(self, image_bytes: bytes) -> io.BytesIO | None:
        """
        将图片字节转换为 JPEG 格式，并确保大小不超过配置的限制。
        在线程中执行以避免阻塞。
        """
        def _process():
            try:
                img = Image.open(io.BytesIO(image_bytes))
                if img.mode != "RGB":
                    img = img.convert("RGB")

                buffer = io.BytesIO()

                # 尝试 1: 质量 85 (JPEG)
                img.save(buffer, format="JPEG", quality=85)
                if buffer.tell() <= self.max_file_size_bytes:
                    buffer.seek(0)
                    return buffer

                # 尝试 2: 质量 70 (JPEG)
                buffer.seek(0); buffer.truncate(0) # 重置 buffer
                img.save(buffer, format="JPEG", quality=70)
                if buffer.tell() <= self.max_file_size_bytes:
                    buffer.seek(0)
                    return buffer

                # 尝试 3: 质量 50 (JPEG)
                buffer.seek(0); buffer.truncate(0) # 重置 buffer
                img.save(buffer, format="JPEG", quality=50)
                if buffer.tell() <= self.max_file_size_bytes:
                    buffer.seek(0)
                    return buffer

                # 失败
                logger.warning(f"Image size ({buffer.tell()} bytes) exceeds {self.max_file_size_mb}MB even after compression (quality 50).")
                return None

            except Exception as e:
                logger.error(f"Image processing (PIL) failed: {e}")
                return None

        # 在线程池中运行 PIL 处理
        return await asyncio.to_thread(_process)

    async def call_api(self, image_buffer: io.BytesIO, config: dict) -> bytes | None:
        """
        调用 Vercel API 并返回生成的图片字节。
        """
        files = {"image": ("image.jpg", image_buffer, "image/jpeg")}
        data = {"config": json.dumps(config)}

        try:
            response = await self.client.post(self.api_url, files=files, data=data, timeout=30)

            if response.status_code == 200:
                return response.content
            else:
                logger.error(f"API Error: Status {response.status_code}, Response: {response.text}")
                return None
        except httpx.TimeoutException:
            logger.error("API request timed out.")
            raise # 抛出异常，由 handler 捕获并回复用户
        except Exception as e:
            logger.error(f"API request failed: {e}")
            raise # 抛出异常

    @filter.command("onelast")
    async def onelast_command(self, event: AstrMessageEvent, config_str: str | None = None):
        """
        命令处理器:
        /onelast - 使用默认配置
        /onelast {'hajimei':True} - 使用自定义配置
        """
        try:
            if not self.api_url:
                yield event.plain_result("插件未配置 API URL，请联系管理员在后台配置。")
                return

            # 1. 获取图片
            images = await self.get_image_from_direct_event(event)
            if not images:
                yield event.plain_result("请发送 /onelast 并附带图片，或回复一张图片。")
                return

            # 2. 解析配置
            current_config = self.default_params.copy()

            # 检查 config_str 是否为 None
            if config_str:
                user_config_str = config_str.strip()
                try:
                    # 将 ast.literal_eval 替换为 json.loads
                    user_params = json.loads(user_config_str)
                    if not isinstance(user_params, dict):
                        raise ValueError("Input is not a dictionary")
                    # 合并配置，用户输入覆盖默认配置
                    current_config.update(user_params)
                except (json.JSONDecodeError, ValueError) as e: # 明确捕获 JSON 和 Value 错误
                    logger.warning(f"Failed to parse user config '{user_config_str}': {e}")
                    # 要求严格的 JSON 格式 (键和字符串都用双引号)
                    yield event.plain_result('参数格式错误，请提供有效的JSON字典字符串，例如：\n/onelast {"watermark":true,"hajimei":true}')
                    return

            # 3. 处理图片
            if len(images) > self.max_images:
                yield event.plain_result(f"检测到 {len(images)} 张图片，超过最大数量 {self.max_images}。仅处理前 {self.max_images} 张。")
                images = images[:self.max_images]
            else:
                yield event.plain_result(f"收到 {len(images)} 张图片，开始生成，请稍候...")

            for i, img_comp in enumerate(images):
                try:
                    # 4. 下载
                    image_bytes = await self.download_image(img_comp)
                    if not image_bytes:
                        yield event.plain_result(f"第 {i+1} 张图片下载失败。")
                        continue

                    # 5. 压缩
                    image_buffer = await self.process_and_compress_image(image_bytes)
                    if not image_buffer:
                        # 使用配置中的大小
                        yield event.plain_result(f"第 {i+1} 张图片处理失败：压缩后仍超过 {self.max_file_size_mb}MB。")
                        continue

                    # 6. 调用 API
                    result_bytes = await self.call_api(image_buffer, current_config)
                    if result_bytes:
                        # 将原始 bytes 编码为 base64 字符串，并使用 base64:// URI
                        result_base64_str = base64.b64encode(result_bytes).decode("utf-8")
                        yield event.chain_result([Comp.Image(file=f"base64://{result_base64_str}")])
                    else:
                        yield event.plain_result(f"第 {i+1} 张图片 API 请求失败，未返回图片。")

                except httpx.TimeoutException:
                    yield event.plain_result(f"第 {i+1} 张图片 API 请求超时。")
                except Exception as e:
                    logger.error(f"Error processing image {i+1}: {e}", exc_info=True)
                    yield event.plain_result(f"第 {i+1} 张图片处理时发生未知错误: {e}")
        except Exception as e:
            logger.error(f"OneLastImagePlugin command failed: {e}", exc_info=True)
            yield event.plain_result("插件运行时发生未知错误，请联系管理员查看日志。")
            event.stop_event()
