import pytest
import MetaTrader5 as mt5
from unittest.mock import AsyncMock, MagicMock, patch
import src.event_processor as ep

@pytest.fixture
def mock_deps():
    deps = {
        'trade_manager': MagicMock(),
        'trade_calculator': MagicMock(),
        'telegram_sender': MagicMock(),
        'state_manager': MagicMock(),
        'mt5_fetcher': MagicMock(),
        'config_service_instance': MagicMock(),
        'duplicate_checker': MagicMock(),
    }

    # Patch mt5_executor with execute_trade returning tuple
    mock_trade_result = MagicMock()
    mock_trade_result.retcode = mt5.TRADE_RETCODE_DONE
    mock_trade_result.order = 123456
    mt5_executor_mock = MagicMock()
    mt5_executor_mock.execute_trade.return_value = (mock_trade_result, 2000.0)
    deps['mt5_executor'] = mt5_executor_mock

    # Patch mt5_fetcher.get_symbol_info to return dummy with volume_min
    class DummySymbolInfo:
        def __init__(self):
            self.volume_min = 0.01
            self.volume_max = 100.0
            self.volume_step = 0.01
            self.digits = 2

    deps['mt5_fetcher'].get_symbol_info.return_value = DummySymbolInfo()

    # Patch telegram_sender async methods
    class DummyConfMsg:
        def __init__(self):
            self.id = 123456
            self.chat_id = -1001234567890

    dummy_conf_msg = DummyConfMsg()

    deps['telegram_sender'].send_message = AsyncMock(return_value=dummy_conf_msg)
    deps['telegram_sender'].send_confirmation_message = AsyncMock(return_value=dummy_conf_msg)
    deps['telegram_sender'].edit_message = AsyncMock(return_value=True)

    deps['duplicate_checker'].add_processed_id = MagicMock()

    # Patch decision_logic.decide to return 3 values
    deps['decision_logic'] = MagicMock()
    deps['decision_logic'].decide.return_value = (True, "Approved", mt5.ORDER_TYPE_BUY_LIMIT)

    # Patch trade_calculator to return a float lot size
    deps['trade_calculator'].calculate_lot_size.return_value = 0.02

    # Patch config_service_instance.getfloat and getboolean to return real values
    deps['config_service_instance'].getfloat.side_effect = lambda *args, **kwargs: 0.0
    def getboolean_side_effect(section, key, fallback=False):
        if key == 'require_market_confirmation':
            return False
        return False
    deps['config_service_instance'].getboolean.side_effect = getboolean_side_effect
    deps['config_service_instance'].get.side_effect = lambda *args, **kwargs: 'sequential_partial_close'

    # Dummy event with .id attribute
    class DummyEvent:
        def __init__(self, id_val):
            self.id = id_val

    deps['dummy_event'] = DummyEvent(12345)

    return deps

@pytest.mark.asyncio
async def test_process_new_signal_runs(mock_deps):
    signal_data = MagicMock()
    signal_data.is_signal = True
    signal_data.action = "BUY"
    signal_data.entry_type = "Pending"
    signal_data.entry_price = "3100-3102"
    signal_data.stop_loss = 3095
    signal_data.take_profits = [3110, 3120]
    signal_data.symbol = "XAUUSD"
    signal_data.sentiment_score = 0.8

    with patch("src.event_processor.DistributedLimitsStrategy") as mock_strategy:
        mock_instance = AsyncMock()
        mock_strategy.return_value = mock_instance
        mock_instance.execute.return_value = None

        await ep.process_new_signal(
            signal_data,
            12345,  # message_id dummy
            mock_deps['state_manager'],
            mock_deps['decision_logic'],
            mock_deps['trade_calculator'],
            mock_deps['mt5_executor'],
            mock_deps['telegram_sender'],
            mock_deps['duplicate_checker'],
            mock_deps['config_service_instance'],
            "TestPrefix",
            mock_deps['mt5_fetcher']
        )
        # mock_instance.execute.assert_awaited()
    
    @pytest.mark.asyncio
    async def test_process_new_signal_autosl_pips(mock_deps):
        # Arrange: signal with missing SL, config enables AutoSL with pip-based SL
        signal_data = MagicMock()
        signal_data.is_signal = True
        signal_data.action = "BUY"
        signal_data.entry_type = "Market"
        signal_data.entry_price = 2000.0
        signal_data.stop_loss = None  # No SL provided
        signal_data.take_profits = [2010.0]
        signal_data.symbol = "XAUUSD"
        signal_data.sentiment_score = 0.8
    
        # Patch config to enable AutoSL and set pip-based SL
        mock_deps['config_service_instance'].getboolean.side_effect = lambda section, key, fallback=False: True if key == 'enable_auto_sl' else fallback
        mock_deps['config_service_instance'].getfloat.side_effect = lambda section, key, fallback=None: 40.0 if key == 'auto_sl_risk_pips' else fallback
    
        # Patch trade_calculator to return a known SL
        mock_deps['trade_calculator'].calculate_sl_from_pips.return_value = 1996.0
    
        # Patch mt5_executor to capture the trade request
        trade_request = {}
        def mock_execute_trade(*args, **kwargs):
            # Capture exec_sl from kwargs or positional args
            nonlocal trade_request
            if args:
                # If exec_sl is a positional argument
                trade_request['exec_sl'] = args[4] if len(args) > 4 else kwargs.get('exec_sl')
            else:
                trade_request['exec_sl'] = kwargs.get('exec_sl')
            mock_result = MagicMock()
            mock_result.retcode = mt5.TRADE_RETCODE_DONE
            mock_result.order = 123456
            return (mock_result, 2000.0)
        mock_deps['mt5_executor'].execute_trade.side_effect = mock_execute_trade
    
        # Act
        await ep.process_new_signal(
            signal_data,
            12345,
            mock_deps['state_manager'],
            mock_deps['decision_logic'],
            mock_deps['trade_calculator'],
            mock_deps['mt5_executor'],
            mock_deps['telegram_sender'],
            mock_deps['duplicate_checker'],
            mock_deps['config_service_instance'],
            "TestPrefix",
            mock_deps['mt5_fetcher']
        )
    
        # Assert
        mock_deps['trade_calculator'].calculate_sl_from_pips.assert_called_once_with(
            "XAUUSD", mt5.ORDER_TYPE_BUY, 2000.0, 40.0
        )
        # The trade request should have exec_sl set to the calculated SL
        assert trade_request['exec_sl'] == 1996.0

@pytest.mark.asyncio
async def test_process_update_runs(mock_deps):
    update_data = MagicMock()
    update_data.update_type = "modify_sltp"
    update_data.target_trade_index = 1
    update_data.new_stop_loss = 3095
    update_data.new_take_profits = [3110, 3120]

    await ep.process_update(
        update_data,
        mock_deps['dummy_event'],  # event with .id attribute
        mock_deps['state_manager'],
        mock_deps['mt5_executor'],
        mock_deps['telegram_sender'],
        mock_deps['duplicate_checker'],
        mock_deps['config_service_instance'],
        "TestPrefix",
        mock_deps['mt5_fetcher'],
        {},  # llm_context dummy
        None  # image_data dummy
    )