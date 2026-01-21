import inspect

from classes.protocol_settings import Registry_Type, protocol_settings

try:
    from pymodbus.client.sync import ModbusSerialClient
except ImportError:
    from pymodbus.client import ModbusSerialClient


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


        # Get the signature of the __init__ method
        init_signature = inspect.signature(ModbusSerialClient.__init__)

        client_str = self.port+"("+str(self.baudrate)+")"

        # Thread-safe client access
        with self._clients_lock:
            if client_str in modbus_base.clients:
                self.client = modbus_base.clients[client_str]
                return

        self._log.debug(f"Creating new client with baud rate: {self.baudrate}")

        if "method" in init_signature.parameters:
            self.client = ModbusSerialClient(method="rtu", port=self.port,
                                        baudrate=int(self.baudrate),
                                        stopbits=1, parity="N", bytesize=8, timeout=2
                                        )
        else:
            self.client = ModbusSerialClient(
                            port=self.port,
                            baudrate=int(self.baudrate),
                            stopbits=1, parity="N", bytesize=8, timeout=2
                            )

        #add to clients (thread-safe)
        with self._clients_lock:
            modbus_base.clients[client_str] = self.client

    def read_registers(self, start, count=1, registry_type : Registry_Type = Registry_Type.INPUT, **kwargs):

        if "unit" not in kwargs:
            kwargs = {"unit": int(self.addresses[0]), **kwargs}

        #compatability
        if self.pymodbus_slave_arg != "unit":
            kwargs["slave"] = kwargs.pop("unit")

        # Use port-specific lock for thread-safe access
        port_lock = self._get_port_lock()
        with port_lock:
            if registry_type == Registry_Type.INPUT:
                return self.client.read_input_registers(address=start, count=count, **kwargs)
            elif registry_type == Registry_Type.HOLDING:
                return self.client.read_holding_registers(address=start, count=count, **kwargs)

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
            self.client.write_register(register, value, **kwargs) #function code 0x06 writes to holding register

    def connect(self):
        # If not connected or during reconnection, try to re-resolve USB port
        # This handles cases where USB device disconnects and reconnects with a different port name
        if not self.connected or self._needs_reconnection:
            old_port = self.port
            # Re-resolve port from serial number specification
            new_port = find_usb_serial_port(self.port_spec)
            if new_port and new_port != old_port:
                self._log.info(f"USB port changed from {old_port} to {new_port}, updating client")
                self.port = new_port
                
                # Close old client if it exists
                old_client_str = old_port+"("+str(self.baudrate)+")"
                with self._clients_lock:
                    if old_client_str in modbus_base.clients:
                        try:
                            old_client = modbus_base.clients[old_client_str]
                            if hasattr(old_client, 'close') and callable(old_client.close):
                                old_client.close()
                            del modbus_base.clients[old_client_str]
                        except Exception as e:
                            self._log.warning(f"Error closing old client: {e}")
                
                # Create new client with new port
                client_str = self.port+"("+str(self.baudrate)+")"
                
                # Check if a client for this port+baudrate already exists
                with self._clients_lock:
                    if client_str in modbus_base.clients:
                        self.client = modbus_base.clients[client_str]
                    else:
                        # Create new client
                        init_signature = inspect.signature(ModbusSerialClient.__init__)
                        self._log.debug(f"Creating new client with baud rate: {self.baudrate}")
                        
                        if "method" in init_signature.parameters:
                            self.client = ModbusSerialClient(method="rtu", port=self.port,
                                                        baudrate=int(self.baudrate),
                                                        stopbits=1, parity="N", bytesize=8, timeout=2
                                                        )
                        else:
                            self.client = ModbusSerialClient(
                                            port=self.port,
                                            baudrate=int(self.baudrate),
                                            stopbits=1, parity="N", bytesize=8, timeout=2
                                            )
                        
                        modbus_base.clients[client_str] = self.client
                        self._log.info(f"Created new client for port {self.port}")
            elif new_port is None:
                # Port not found - device may be disconnected
                self._log.warning(f"USB device with specification '{self.port_spec}' not found, keeping existing port {self.port}")
            elif new_port == old_port:
                # Port unchanged
                self._log.debug(f"USB port unchanged: {self.port}")
        
        self.connected = self.client.connect()
        self._log.info(f"Modbus rtu connected: {self.connected} for {self.transport_name} on port {self.port}")
        if not self.connected:
            self._log.error(f"Failed to connect to {self.transport_name} on port {self.port}")
        super().connect()
