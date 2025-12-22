# Tradedesk


Example private strategy

```python
# In your private repo: my_strategies/secret_algo.py
from tradedesk.strategy import BaseStrategy

class MySecretStrategy(BaseStrategy):
    async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
        # Your proprietary logic here
        pass

# In runner.py
from tradedesk import IGClient
from my_strategies import MySecretStrategy

client = IGClient()
strategy = MySecretStrategy(client, epics=["CS.D.EURUSD.TODAY.IP"])
await strategy.run()
```
