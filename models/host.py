class Host:
    def __init__(self, name, address, port=22):
        self.name = name
        self.address = address
        self.port = port
        self.status = "Unknown"

    def __repr__(self):
        return f"Host({self.name}@{self.address}:{self.port})"