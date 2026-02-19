from tradedesk.events import DomainEvent, event


@event
class ReportingCompleteEvent(DomainEvent):
    pass
