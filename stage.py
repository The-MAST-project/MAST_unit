
import logging
from enum import Enum, Flag
import datetime
from utils import RepeatTimer
from typing import TypeAlias
from mastapi import Mastapi

logger = logging.getLogger('mast.unit.stage')

StageStateType: TypeAlias = "StageState"


class StageActivities(Flag):
    Idle = 0
    Moving = (1 << 1)


class StageStatus:
    is_operational: bool
    position: int
    state: StageStateType
    state_verbal: str
    activities: StageActivities
    activities_verbal: str
    api_methods: list


class StageState(Enum):
    Idle = 0
    In = 1
    Out = 2
    MovingIn = 3
    MovingOut = 4
    Error = 5
    Operational = In
    Parked = Out


class Stage(Mastapi):

    MIN_TICKS = 0
    MAX_TICKS = 50000
    TICKS_WHEN_IN = 100
    TICKS_WHEN_OUT = 30000
    TICKS_PER_SECOND = 1000

    _connected: bool
    _position: int
    state: StageState
    default_initial_state: StageState = StageState.In
    ticks_at_start: int
    ticks_at_target: int
    motion_start_time: datetime
    timer: RepeatTimer
    activities: StageActivities

    def __init__(self):
        self.state = StageState.Idle
        self._connected = False

        for func in [self.connect, self.disconnect, self.startup, self.shutdown, self.status, self.move]:
            Mastapi.api_method(func)

        self.timer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = 'mast.stage'
        self.timer.start()
        self.activities = StageActivities.Idle
        logger.info('initialized')

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value):
        if value:
            try:
                # connect to the controller
                # get the stage location
                # if it is not at the preferred initial stage location (In/Out?)
                #  - move it
                #  - set self.state accordingly
                self.state = StageState.In
                self.position = self.TICKS_WHEN_IN

            except Exception as ex:
                logger.exception(ex)
                self.state = StageState.Error
                raise ex

        self._connected = value
        logger.info(f'connected = {value}')

    def connect(self):
        """
        Connects to the MAST stage controller
        :return:
        """
        self.connected = True

    def disconnect(self):
        """
        Disconnects from the MAST stage controller
        :return:
        """
        self.connected = False

    def startup(self):
        """
        Startup routine for the MAST stage.  Makes it operational
        :return:
        """
        if self.state is not StageState.Operational:
            self.move(StageState.Operational)

    def shutdown(self):
        """
        Shutdown routine for the MAST stage.  Makes it idle
        :return:
        """
        self.move(StageState.Parked)

    @property
    def position(self) -> int:
        return self._position

    @position.setter
    def position(self, value):
        self._position = value

    def status(self) -> StageStatus:
        """
        Returns the status of the MAST stage
        :return:
        """
        st = StageStatus()
        st.state = self.state
        st.state_verbal = st.state.name
        st.position = self.position
        st.is_operational = st.state == StageState.Operational
        st.activities = self.activities
        st.activities_verbal = st.activities.name
        return st

    def ontimer(self):
        if self.state == StageState.MovingIn or self.state == StageState.MovingOut:
            dt = (datetime.datetime.now() - self.motion_start_time).seconds
            if self.state == StageState.MovingOut:
                pos = self.ticks_at_start + dt * self.TICKS_PER_SECOND
                if pos > self.TICKS_WHEN_OUT:
                    pos = self.TICKS_WHEN_OUT
                    self.state = StageState.Out
                    self.activities &= ~StageActivities.Moving
            else:
                pos = self.ticks_at_start - dt * self.TICKS_PER_SECOND
                if pos <= self.TICKS_WHEN_IN:
                    pos = self.TICKS_WHEN_IN
                    self.state = StageState.In
                    self.activities &= ~StageActivities.Moving

            self.position = pos
            logger.info(f'ontimer: position={self.position}')

    def move(self, where: StageState):
        if not self.connected:
            raise 'Not connected'

        if self.state == where:
            logger.info(f'move: already {where}')
            return

        self.activities |= StageActivities.Moving
        self.state = StageState.MovingIn if where == StageState.In else StageState.MovingOut
        self.ticks_at_start = self.position
        self.motion_start_time = datetime.datetime.now()
        logger.info(f'move: at {self.position} started moving, state={self.state}')
