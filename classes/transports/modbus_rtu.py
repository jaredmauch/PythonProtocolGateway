import inspect

from classes.protocol_settings import Registry_Type, protocol_settings

try:
    from pymodbus.client.sync import ModbusSerialClient
except ImportError:
    from pymodbus.client import ModbusSerialClient

from pymodbus.exceptions import ModbusIOException

from configparser import SectionProxy

from defs.common import find_usb_serial_port, get_usb_serial_port_info, strtoint

from .modbus_base import modbus_base


class modbus_rtu(modbus_base):
    port : str = "/dev/ttyUSB0"
    addresses : list[int] = []
    baudrate : int = 9600
    client : ModbusSerialClient

    pymodbus_slave_arg = "unit"

    def __init__(self, settings : SectionProxy, protocolSettings : protocol_settings = None):
        super().__init__(settings, protocolSettings=protocolSettings)

        # Store the original port specification from config (may be serial number format)
        self.port_spec = settings.get("port", "")
        if not self.port_spec:
            raise ValueError("Port is not set")

        self.port = find_usb_serial_port(self.port_spec)
        if not self.port:
            raise ValueError("Port is not valid / not found")

        print("Serial Port : " + self.port + " = ", get_usb_serial_port_info(self.port)) #print for config convience

        if "baud" in self.protocolSettings.settings:
            self.baudrate = strtoint(self.protocolSettings.settings["baud"])
        #todo better baud/baudrate alias handling
        self.baudrate = settings.getint("baudrate", self.baudrate)

        address : int = settings.getint("address", 0)
        self.addresses = [address]

        # pymodbus compatability; unit was renamed to address
        if "slave" in inspect.signature(ModbusSerialClient.read_holding_registers).parameters:
            self.pymodbus_slave_arg = "slave"

        client_str = self._client_key(self.port)
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return

        self.client = self._create_modbus_client(self.port)
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def _client_key(self, port: str) -> str:
        return port + "(" + str(self.baudrate) + ")"

    def _should_probe_usb_port(self) -> bool:
        return bool(getattr(self, "port_spec", None)) and self.port_spec.startswith("[")

    def _create_modbus_client(self, port: str) -> ModbusSerialClient:
        self._log.debug(f"Creating new client with baud rate: {self.baudrate} on {port}")
        init_signature = inspect.signature(ModbusSerialClient.__init__)
        if "method" in init_signature.parameters:
            return ModbusSerialClient(
                method="rtu",
                port=port,
                baudrate=int(self.baudrate),
                stopbits=1,
                parity="N",
                bytesize=8,
                timeout=2,
            )
        return ModbusSerialClient(
            port=port,
            baudrate=int(self.baudrate),
            stopbits=1,
            parity="N",
            bytesize=8,
            timeout=2,
        )

    def _remove_client_for_port(self, port: str):
        client_str = self._client_key(port)
        with self._clients_lock:
            if client_str not in modbus_base.clients:
                return
            try:
                client = modbus_base.clients[client_str]
                if hasattr(client, "close") and callable(client.close):
                    client.close()
            except Exception as e:
                self._log.warning(f"Error closing modbus client for {port}: {e}")
            del modbus_base.clients[client_str]

    def _get_or_create_client(self, port: str) -> ModbusSerialClient:
        client_str = self._client_key(port)
        with self._clients_lock:
            if client_str in modbus_base.clients:
                return modbus_base.clients[client_str]
        client = self._create_modbus_client(port)
        with self._clients_lock:
            modbus_base.clients[client_str] = client
        return client

    def _refresh_usb_port(self, force_recreate: bool = False) -> bool:
        """Re-probe USB adapter by serial number and refresh the modbus client."""
        old_port = self.port

        if self._should_probe_usb_port():
            new_port = find_usb_serial_port(self.port_spec)
            if not new_port:
                self._log.warning(
                    f"USB device with specification '{self.port_spec}' not found, keeping existing port {self.port}"
                )
                if force_recreate:
                    self._remove_client_for_port(old_port)
                    self.client = self._get_or_create_client(self.port)
                    return True
                return False

            port_changed = new_port != old_port
            if port_changed:
                self._log.info(f"USB port changed from {old_port} to {new_port}, updating client")
                self.port = new_port
            elif force_recreate:
                self._log.info(f"Re-probing USB adapter for {self.transport_name} on {self.port}")

            if port_changed or force_recreate:
                if port_changed:
                    self._remove_client_for_port(old_port)
                else:
                    self._remove_client_for_port(self.port)
                self.client = self._get_or_create_client(self.port)
                return True

            self._log.debug(f"USB port unchanged: {self.port}")
            return False

        if force_recreate:
            self._log.info(f"Recreating modbus client for {self.transport_name} on {self.port}")
            self._remove_client_for_port(self.port)
            self.client = self._get_or_create_client(self.port)
            return True

        return False

    def _handle_communication_error(self, error: Exception):
        """Close stale connection, re-probe USB adapter, and reconnect."""
        self._log.warning(f"Communication error for {self.transport_name}: {error}")
        self.connected = False
        self._needs_reconnection = True

        try:
            if hasattr(self.client, "close") and callable(self.client.close):
                self.client.close()
        except Exception:
            pass

        self._refresh_usb_port(force_recreate=True)
        self.connected = self.client.connect()
        if self.connected:
            self._log.info(f"Reconnected {self.transport_name} on port {self.port}")
        else:
            self._log.error(f"Failed to reconnect {self.transport_name} on port {self.port}")

    def read_registers(self, start, count=1, registry_type : Registry_Type = Registry_Type.INPUT, **kwargs):

        if "unit" not in kwargs:
            kwargs = {"unit": int(self.addresses[0]), **kwargs}

        #compatability
        if self.pymodbus_slave_arg != "unit":
            kwargs["slave"] = kwargs.pop("unit")

        # Use port-specific lock for thread-safe access
        port_lock = self._get_port_lock()
        with port_lock:
            try:
                if registry_type == Registry_Type.INPUT:
                    return self.client.read_input_registers(address=start, count=count, **kwargs)
                elif registry_type == Registry_Type.HOLDING:
                    return self.client.read_holding_registers(address=start, count=count, **kwargs)
            except (OSError, ModbusIOException) as e:
                self._handle_communication_error(e)
                raise

    def write_register(self, register : int, value : int, **kwargs):
        if not self.write_enabled:
            return

        if "unit" not in kwargs:
            kwargs = {"unit": self.addresses[0], **kwargs}

        #compatability
        if self.pymodbus_slave_arg != "unit":
            kwargs["slave"] = kwargs.pop("unit")

        # Use port-specific lock for thread-safe access
        port_lock = self._get_port_lock()
        with port_lock:
            try:
                self.client.write_register(register, value, **kwargs) #function code 0x06 writes to holding register
            except (OSError, ModbusIOException) as e:
                self._handle_communication_error(e)
                raise

    def connect(self):
        if not self.connected or self._needs_reconnection:
            self._refresh_usb_port(force_recreate=self._needs_reconnection)

        self.connected = self.client.connect()
        self._log.info(f"Modbus rtu connected: {self.connected} for {self.transport_name} on port {self.port}")
        if not self.connected:
            self._log.error(f"Failed to connect to {self.transport_name} on port {self.port}")
        super().connect()
