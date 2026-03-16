from dataclasses import dataclass, field
from typing import Optional
 
 
@dataclass
class ContainerDetails:
    challenge: Optional[str] = None
    team: Optional[str] = None
    token: Optional[str] = None
    container_index: Optional[str] = None 
    url: Optional[str] = None
    image: Optional[object] = None
    status: Optional[str] = None
 