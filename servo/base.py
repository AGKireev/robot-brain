# ======================================================================
# IMPORTANT UPDATE NOTES:
#
# This code was originally written using the legacy 'Adafruit_PCA9685' library
# and 'pwm.set_pwm(...)' calls to control servo position with raw PWM values.
# We are now using the newer Adafruit CircuitPython libraries:
#   - adafruit_pca9685
#   - adafruit_motor.servo
#
# The original code controlled servo positions by sending values like 100 to 520
# to pwm.set_pwm(...). Those values corresponded to a certain pulse width that mapped
# linearly to angles from 0 to 180 degrees. We must preserve the exact same functionality,
# behavior, speed, and range to avoid damaging the devices and maintain external script compatibility.
#
# Original key parameters:
#   ctrlRangeMin = 100 steps
#   ctrlRangeMax = 520 steps
#   angleRange   = 180 degrees
#
# This means:
#   100 steps -> 0 degrees
#   520 steps -> 180 degrees
#
# We will keep all internal arrays (init_positions, goalPos, etc.) and computations in terms of these "PWM step" values.
# Only at the point of actually commanding the servo, we will convert these step values to an angle for the servo library.
#
# Conversion from steps to angle:
#   angle = ((PWM_steps - ctrlRangeMin) / (ctrlRangeMax - ctrlRangeMin)) * angleRange
#   angle = ((steps - 100) / (420)) * 180
#
# Since we are using adafruit_motor.servo, we specify min_pulse and max_pulse so that:
#   angle=0° -> ~488us
#   angle=180° -> ~2538us
# This matches original scaling. Each "step" approx. 4.88us, so 100 steps ~488us and 520 steps ~2538us.
# ======================================================================
import time
import threading
import logging
from typing import List, Union, Optional, Dict, Any
from board import SCL, SDA
import busio
import adafruit_pca9685
from adafruit_motor import servo

from system import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize I2C and PCA9685
i2c = busio.I2C(SCL, SDA)
pca = adafruit_pca9685.PCA9685(i2c)
pca.frequency = 50

# Define pulse width range in microseconds for 0 to 180 degrees
MIN_PULSE_US = 500  # 488
MAX_PULSE_US = 2500 # 2538

# Load servo configuration
servo_config = config.read("servos")

def create_servo(channel: int) -> servo.Servo:
    """Create a servo instance for a given channel with proper PWM configuration."""
    pwm = pca.channels[channel]
    return servo.Servo(pwm, min_pulse=MIN_PULSE_US, max_pulse=MAX_PULSE_US)

class ServoCtrl(threading.Thread):
    def __init__(self, servo_group: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if servo_group not in servo_config:
            raise ValueError(f"Invalid servo group: {servo_group}. Must be one of {list(servo_config.keys())}")
        
        self.servo_group = servo_group
        self.pwm_channels = []
        self.init_positions = {}
        
        # Initialize control arrays first
        self.sc_direction = {}
        self.goal_positions = {}
        self.current_positions = {}
        self.buffer_positions = {}
        self.last_positions = {}
        self.ing_goal = {}
        self.min_positions = {}
        self.max_positions = {}
        self.sc_speed = {}
        
        # Initialize servos based on group
        if servo_group == 'legs':
            for leg_name, leg_data in servo_config['legs'].items():
                if leg_name == 'limits':  # Skip the limits entry
                    continue
                channels = leg_data['channels']
                positions = leg_data['center_position']
                directions = leg_data['direction']
                
                # Add both horizontal and vertical channels for this leg
                h_channel = channels['horizontal']
                v_channel = channels['vertical']
                self.pwm_channels.extend([h_channel, v_channel])
                
                # Set initial positions
                self.init_positions[h_channel] = positions['horizontal']
                self.init_positions[v_channel] = positions['vertical']
                
                # Set directions
                self.sc_direction[h_channel] = directions['horizontal']
                self.sc_direction[v_channel] = directions['vertical']
                
                logger.debug(f"Initialized leg {leg_name} - Channels: {h_channel},{v_channel}")
        else:  # camera
            channels = servo_config['camera']['channels']
            positions = servo_config['camera']['center_position']
            directions = servo_config['camera']['direction']
            self.pwm_channels = [channels['horizontal'], channels['vertical']]
            self.init_positions[channels['horizontal']] = positions['horizontal']
            self.init_positions[channels['vertical']] = positions['vertical']
            self.sc_direction[channels['horizontal']] = directions['horizontal']
            self.sc_direction[channels['vertical']] = directions['vertical']
            logger.debug(f"Initialized camera servos - Channels: {self.pwm_channels}")
        
        # Create servo instances only for the channels we need
        self.servos = {
            channel: create_servo(channel)
            for channel in self.pwm_channels
        }
        logger.info(f"Created servo instances for channels: {list(self.servos.keys())}")
        
        # Initialize default values for control arrays
        for channel in self.pwm_channels:
            init_pos = self.init_positions[channel]  # Get position from config
            self.goal_positions[channel] = init_pos
            self.current_positions[channel] = init_pos
            self.buffer_positions[channel] = float(init_pos)
            self.last_positions[channel] = init_pos
            self.ing_goal[channel] = init_pos
            
            # Set channel-specific limits from config
            if servo_group == 'camera':
                limits = servo_config['camera']['limits']
                if channel == channels['horizontal']:
                    self.min_positions[channel] = limits['horizontal']['min']
                    self.max_positions[channel] = limits['horizontal']['max']
                else:  # vertical
                    self.min_positions[channel] = limits['vertical']['min']
                    self.max_positions[channel] = limits['vertical']['max']
            else:  # legs
                limits = servo_config['legs']['limits']
                self.min_positions[channel] = limits['min']
                self.max_positions[channel] = limits['max']
            
            self.sc_speed[channel] = 0
            
        logger.info("Channel limits:")
        for channel in self.pwm_channels:
            logger.info(f"Channel {channel}: min={self.min_positions[channel]}, max={self.max_positions[channel]}")

        # Set global control range from leg limits since they define the full servo range
        leg_limits = servo_config['legs']['limits']
        self.ctrl_range_max: int = leg_limits['max']
        self.ctrl_range_min: int = leg_limits['min']
        self.angle_range: int = 180

        '''
        scMode: 'init' 'auto' 'certain' 'quick' 'wiggle'
        '''
        self.sc_mode: str = 'auto'
        self.sc_time: float = 2.0
        self.sc_steps: int = 30
        self.sc_delay: float = 0.037
        self.sc_move_time: float = 0.037

        self.goal_update: int = 0
        self.wiggle_id: int = 0
        self.wiggle_direction: int = 1

        self.running = threading.Event()
        self.running.clear()
        
        logger.info(f"ServoCtrl initialized for group '{servo_group}' with channels: {self.pwm_channels}")
        logger.debug(f"Initial positions: {self.init_positions}")
        logger.debug(f"Servo directions: {self.sc_direction}")

    def pause(self) -> None:
        logger.info("ServoCtrl: pause")
        self.running.clear()

    def resume(self) -> None:
        logger.info("ServoCtrl: resume")
        self.running.set()

    def pwm_to_angle(self, pwm: int) -> int:
        """Convert PWM value (100-520) to angle (0-180)."""
        angle = int(max(0, min(int((pwm - self.ctrl_range_min) / (self.ctrl_range_max - self.ctrl_range_min) * self.angle_range), 180)))
        logger.debug(f"Converting PWM {pwm} to angle {angle}")
        return angle

    def set_servo_pwm(self, channel: int, pwm: int) -> None:
        """Set servo position using PWM value."""
        if channel not in self.servos:
            logger.error(f"Invalid channel {channel} for servo group {self.servo_group}")
            raise ValueError(f"Channel {channel} not in servo group {self.servo_group}")
            
        if self.min_positions[channel] <= pwm <= self.max_positions[channel]:
            angle = self.pwm_to_angle(pwm)
            logger.debug(f"Setting servo {channel} to PWM {pwm} (angle: {angle}°)")
            self.servos[channel].angle = angle
            self.current_positions[channel] = pwm
            logger.info(f"Servo {channel} position updated - PWM: {pwm}, Angle: {angle}°")
        else:
            logger.error(f"PWM value {pwm} out of range [{self.min_positions[channel]}, {self.max_positions[channel]}] for channel {channel}")
            raise ValueError(f"PWM value {pwm} out of range for channel {channel}. Must be between {self.min_positions[channel]} and {self.max_positions[channel]}")

    def validate_servo_ids(self, servo_ids: List[int]) -> None:
        """Validate that all servo IDs exist in the current group."""
        invalid_servos = [i for i in servo_ids if i not in self.pwm_channels]
        if invalid_servos:
            raise ValueError(f"Servos {invalid_servos} are not in the current servo group {self.servo_group}")

    def adjust_servo_positions(self, servo_ids: List[int], direction: int, steps: int) -> Dict[str, Any]:
        """
        Adjust servo positions by the specified number of steps in the given direction.
        
        Args:
            servo_ids: List of servo IDs to adjust
            direction: 1 for increment, -1 for decrement
            steps: Number of steps to adjust (positive integer)
            
        Returns:
            Dict containing operation status and current positions
        """
        MAX_STEPS = 20  # Safety limit for maximum steps at once
        
        # Validate inputs
        self.validate_servo_ids(servo_ids)
        if direction not in (-1, 1):
            raise ValueError("Direction must be 1 (increment) or -1 (decrement)")
        if not isinstance(steps, int) or steps <= 0:
            raise ValueError("Steps must be a positive integer")
        if steps > MAX_STEPS:
            raise ValueError(f"Steps cannot exceed {MAX_STEPS}")
            
        # Check if movement would exceed limits for any servo
        positions_after = {}
        for servo_id in servo_ids:
            new_pos = self.current_positions[servo_id] + (direction * steps)
            if not (self.min_positions[servo_id] <= new_pos <= self.max_positions[servo_id]):
                raise ValueError(
                    f"Movement would exceed limits for servo {servo_id}. "
                    f"Current: {self.current_positions[servo_id]}, "
                    f"After: {new_pos}, "
                    f"Limits: [{self.min_positions[servo_id]}, {self.max_positions[servo_id]}]"
                )
            positions_after[servo_id] = new_pos
            
        # Apply the changes
        for servo_id, new_pos in positions_after.items():
            self.set_servo_pwm(servo_id, new_pos)
            
        return {
            "status": "ok",
            "positions": {str(k): v for k, v in self.current_positions.items() if k in servo_ids}
        }

    def save_current_positions(self, servo_ids: List[int]) -> Dict[str, Any]:
        """
        Save current positions of specified servos as their new center positions in config.
        
        Args:
            servo_ids: List of servo IDs to save
            
        Returns:
            Dict containing operation status
        """
        self.validate_servo_ids(servo_ids)
        
        # Update config with new center positions
        for servo_id in servo_ids:
            current_pos = self.current_positions[servo_id]
            
            # Find the leg and axis (horizontal/vertical) for this servo
            for leg_name, leg_data in servo_config['legs'].items():
                if leg_name == 'limits':
                    continue
                    
                channels = leg_data['channels']
                if channels['horizontal'] == servo_id:
                    leg_data['center_position']['horizontal'] = current_pos
                    break
                elif channels['vertical'] == servo_id:
                    leg_data['center_position']['vertical'] = current_pos
                    break
            else:
                # Check camera servos if not found in legs
                camera_channels = servo_config['camera']['channels']
                if camera_channels['horizontal'] == servo_id:
                    servo_config['camera']['center_position']['horizontal'] = current_pos
                elif camera_channels['vertical'] == servo_id:
                    servo_config['camera']['center_position']['vertical'] = current_pos
                    
        # Save updated config
        config.write("servos", None, servo_config)
        
        return {
            "status": "ok",
            "saved_positions": {str(k): v for k, v in self.current_positions.items() if k in servo_ids}
        }

    def center_servos(self, servo_ids: List[int]) -> Dict[str, Any]:
        """
        Move specified servos to their center positions from config.
        
        Args:
            servo_ids: List of servo IDs to center
            
        Returns:
            Dict containing operation status and new positions
        """
        self.validate_servo_ids(servo_ids)
        
        # Move each servo to its center position
        for servo_id in servo_ids:
            self.set_servo_pwm(servo_id, self.init_positions[servo_id])
            
        return {
            "status": "ok",
            "positions": {str(k): v for k, v in self.current_positions.items() if k in servo_ids}
        }

    def reset_servos(self, servo_ids: List[int]) -> Dict[str, Any]:
        """
        Reset specified servos to factory default center position (300).
        
        Args:
            servo_ids: List of servo IDs to reset
            
        Returns:
            Dict containing operation status and new positions
        """
        self.validate_servo_ids(servo_ids)
        DEFAULT_CENTER = 300
        
        # Move servos to default center and update config
        for servo_id in servo_ids:
            # Update servo position
            self.set_servo_pwm(servo_id, DEFAULT_CENTER)
            
            # Update config
            for leg_name, leg_data in servo_config['legs'].items():
                if leg_name == 'limits':
                    continue
                    
                channels = leg_data['channels']
                if channels['horizontal'] == servo_id:
                    leg_data['center_position']['horizontal'] = DEFAULT_CENTER
                    break
                elif channels['vertical'] == servo_id:
                    leg_data['center_position']['vertical'] = DEFAULT_CENTER
                    break
            else:
                # Check camera servos
                camera_channels = servo_config['camera']['channels']
                if camera_channels['horizontal'] == servo_id:
                    servo_config['camera']['center_position']['horizontal'] = DEFAULT_CENTER
                elif camera_channels['vertical'] == servo_id:
                    servo_config['camera']['center_position']['vertical'] = DEFAULT_CENTER
        
        # Save updated config
        config.write("servos", None, servo_config)
        
        return {
            "status": "ok",
            "positions": {str(k): v for k, v in self.current_positions.items() if k in servo_ids}
        }

    def move_init(self, ids: Union[List[int], int, None] = None) -> None:
        """Initialize servos to their default positions."""
        if ids is None:  # Initialize all servos in group
            ids = list(self.pwm_channels)
            logger.info(f"Initializing all servos in group {self.servo_group}: {ids}")
        elif isinstance(ids, int):  # Single servo
            if ids not in self.pwm_channels:
                logger.error(f"Invalid servo {ids} for group {self.servo_group}")
                raise ValueError(f"Servo {ids} is not in the current servo group {self.servo_group}")
            ids = [ids]
            logger.info(f"Initializing single servo {ids[0]}")
        else:  # List of servos
            invalid_servos = [i for i in ids if i not in self.pwm_channels]
            if invalid_servos:
                logger.error(f"Invalid servos {invalid_servos} for group {self.servo_group}")
                raise ValueError(f"Servos {invalid_servos} are not in the current servo group {self.servo_group}")
            logger.info(f"Initializing servos: {ids}")

        for i in ids:
            logger.debug(f"Setting servo {i} to initial position {self.init_positions[i]}")
            self.set_servo_pwm(i, self.init_positions[i])
            self.last_positions[i] = self.init_positions[i]
            self.current_positions[i] = self.init_positions[i]
            self.buffer_positions[i] = float(self.init_positions[i])
            self.goal_positions[i] = self.init_positions[i]

        self.sc_mode = 'init'
        self.pause()
        logger.info("Servo initialization complete")

    def set_init_position(self, id: int, init_input: int, move_to: bool = False) -> None:
        """
        Updates the initial position of a specific servo.

        Args:
            id (int): The servo ID.
            init_input (int): The new initial position (PWM value).
            move_to (bool): If True, moves the servo to the updated initial position immediately.
        """
        if id not in self.pwm_channels:
            raise ValueError(f"Servo {id} is not in the current servo group {self.servo_group}")
            
        if not (self.min_positions[id] <= init_input <= self.max_positions[id]):
            raise ValueError(f"Invalid initial position {init_input} for servo {id}. Must be between {self.min_positions[id]} and {self.max_positions[id]}")
            
        self.init_positions[id] = init_input
        if move_to:
            self.set_servo_pwm(id, init_input)

    def pos_update(self) -> None:
        self.goal_update = 1
        for channel in self.pwm_channels:
            self.last_positions[channel] = self.current_positions[channel]
        self.goal_update = 0

    def speed_update(self, ids: List[int], speeds: List[int]) -> None:
        for i, speed in zip(ids, speeds):
            if i not in self.pwm_channels:
                raise ValueError(f"Channel {i} not in servo group {self.servo_group}")
            self.sc_speed[i] = speed

    def move_auto(self) -> None:
        for channel in self.pwm_channels:
            self.ing_goal[channel] = self.goal_positions[channel]

        for step in range(self.sc_steps):
            for channel in self.pwm_channels:
                if not self.goal_update:
                    delta = (self.goal_positions[channel] - self.last_positions[channel]) / self.sc_steps
                    self.current_positions[channel] = int(round(self.last_positions[channel] + delta * (step + 1)))
                    self.set_servo_pwm(channel, self.current_positions[channel])
            time.sleep(self.sc_time / self.sc_steps)
        self.pos_update()
        self.pause()

    def move_cert(self) -> None:
        for channel in self.pwm_channels:
            self.ing_goal[channel] = self.goal_positions[channel]
            self.buffer_positions[channel] = self.last_positions[channel]

        while any(self.current_positions[channel] != self.goal_positions[channel] for channel in self.pwm_channels):
            for channel in self.pwm_channels:
                if self.last_positions[channel] < self.goal_positions[channel]:
                    self.buffer_positions[channel] += self.sc_speed[channel] / (1 / self.sc_delay)
                elif self.last_positions[channel] > self.goal_positions[channel]:
                    self.buffer_positions[channel] -= self.sc_speed[channel] / (1 / self.sc_delay)
                self.current_positions[channel] = int(round(self.buffer_positions[channel]))
                self.set_servo_pwm(channel, self.current_positions[channel])
            time.sleep(self.sc_delay - self.sc_move_time)
        self.pos_update()
        self.pause()

    def pwm_gen_out(self, angle: float) -> int:
        return int(round((self.ctrl_range_max - self.ctrl_range_min) / self.angle_range * angle, 0))

    def set_auto_time(self, time: float) -> None:
        self.sc_time = time

    def set_delay(self, delay: float) -> None:
        self.sc_delay = delay

    def auto_speed(self, ids: List[int], angles: List[float]) -> None:
        # Validate all IDs before making any changes
        invalid_servos = [i for i in ids if i not in self.pwm_channels]
        if invalid_servos:
            raise ValueError(f"Servos {invalid_servos} are not in the current servo group {self.servo_group}")

        self.sc_mode = 'auto'
        self.goal_update = 1
        for i, angle in zip(ids, angles):
            target = self.init_positions[i] + self.pwm_gen_out(angle) * self.sc_direction[i]
            self.goal_positions[i] = max(self.min_positions[i], min(target, self.max_positions[i]))
        self.goal_update = 0
        self.resume()

    def cert_speed(self, ids: List[int], angles: List[float], speeds: List[int]) -> None:
        # Validate all IDs before making any changes
        invalid_servos = [i for i in ids if i not in self.pwm_channels]
        if invalid_servos:
            raise ValueError(f"Servos {invalid_servos} are not in the current servo group {self.servo_group}")

        self.sc_mode = 'certain'
        self.goal_update = 1
        for i, angle in zip(ids, angles):
            target = self.init_positions[i] + self.pwm_gen_out(angle) * self.sc_direction[i]
            self.goal_positions[i] = max(self.min_positions[i], min(target, self.max_positions[i]))
        self.speed_update(ids, speeds)
        self.goal_update = 0
        self.resume()

    def move_wiggle(self) -> None:
        """Execute wiggle movement for a single servo."""
        logger.info(f"Starting wiggle movement for servo {self.wiggle_id}")
        while self.running.is_set():
            try:
                delta = self.wiggle_direction * self.sc_speed[self.wiggle_id] / (1 / self.sc_delay)
                new_pos = self.buffer_positions[self.wiggle_id] + delta * self.sc_direction[self.wiggle_id]
                
                # Check if we would exceed limits
                if new_pos > self.max_positions[self.wiggle_id]:
                    logger.info(f"Servo {self.wiggle_id} reached maximum position {self.max_positions[self.wiggle_id]}")
                    self.pause()
                    break
                elif new_pos < self.min_positions[self.wiggle_id]:
                    logger.info(f"Servo {self.wiggle_id} reached minimum position {self.min_positions[self.wiggle_id]}")
                    self.pause()
                    break
                
                self.buffer_positions[self.wiggle_id] = new_pos
                self.current_positions[self.wiggle_id] = int(round(new_pos))
                logger.debug(f"Wiggle update - Servo: {self.wiggle_id}, Position: {self.current_positions[self.wiggle_id]}")
                self.set_servo_pwm(self.wiggle_id, self.current_positions[self.wiggle_id])
                time.sleep(self.sc_delay - self.sc_move_time)
            except Exception as e:
                logger.error(f"Error in wiggle movement: {e}")
                self.pause()
                break

    def stop_wiggle(self) -> None:
        """Stop wiggle movement and update position."""
        logger.info(f"Stopping wiggle movement for servo {self.wiggle_id}")
        self.pause()
        self.pos_update()

    def single_servo(self, id: int, direction: int, speed: int) -> None:
        """Control movement of a single servo."""
        if id not in self.pwm_channels:
            logger.error(f"Invalid servo {id} for group {self.servo_group}")
            raise ValueError(f"Servo {id} is not in the current servo group {self.servo_group}")
        
        logger.info(f"Starting single servo movement - ID: {id}, Direction: {direction}, Speed: {speed}")
        self.wiggle_id = id
        self.wiggle_direction = direction
        self.sc_speed[id] = speed
        self.sc_mode = 'wiggle'
        self.resume()

    def move_angle(self, id: int, angle: float) -> None:
        if id not in self.pwm_channels:
            raise ValueError(f"Servo {id} is not in the current servo group {self.servo_group}")
        pwm = self.init_positions[id] + self.pwm_gen_out(angle) * self.sc_direction[id]
        self.current_positions[id] = max(self.min_positions[id], min(int(pwm), self.max_positions[id]))
        self.last_positions[id] = self.current_positions[id]
        self.set_servo_pwm(id, self.current_positions[id])

    def sc_move(self) -> None:
        if self.sc_mode == 'init':
            self.move_init()
        elif self.sc_mode == 'auto':
            self.move_auto()
        elif self.sc_mode == 'certain':
            self.move_cert()
        elif self.sc_mode == 'wiggle':
            self.move_wiggle()

    def set_pwm(self, id: int, pwm: int) -> None:
        if id not in self.pwm_channels:
            raise ValueError(f"Servo {id} is not in the current servo group {self.servo_group}")
        self.last_positions[id] = pwm
        self.current_positions[id] = pwm
        self.buffer_positions[id] = float(pwm)
        self.goal_positions[id] = pwm
        self.set_servo_pwm(id, pwm)
        self.pause()

    def shutdown(self) -> None:
        """Gracefully shut down the servo controller."""
        logger.info(f"Shutting down ServoCtrl for group {self.servo_group}...")
        self.pause()
        for channel, servo in self.servos.items():
            logger.debug(f"Disabling servo on channel {channel}")
            servo.angle = None
        logger.info("ServoCtrl shut down successfully.")

    def run(self) -> None:
        while True:
            self.running.wait()
            self.sc_move()
            # Add a small delay to prevent CPU spinning when idle
            time.sleep(0.01)  # 10ms delay is short enough to not affect responsiveness
