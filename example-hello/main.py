from mediaclaw_plugins.sdk import PluginBase


class ExampleHello(PluginBase):
    def on_enable(self) -> None:
        if self.host is not None:
            self.host.logger.info("示例插件已启用")


plugin = ExampleHello()
