# standard libraries
from typing import Optional, Dict

# ROS standard libraries
import rclpy
from rclpy.node import Node

# 3rd party libraries
import numpy as np

# ros2 messages
from std_msgs.msg import Bool, Float64MultiArray, MultiArrayDimension

# ros2 services
from std_srvs.srv import Trigger

# custom imports
from . import interrogator


class FBGInterrogatorNode( Node ):
    # PARAMETER NAMES
    param_names = {
            'ip'         : 'interrogator.ip_address',
            'ref_wl'     : 'sensor.CH{:d}.reference',
            'num_samples': 'sensor.num_samples',
            }
    SM130_PORT = 1852

    def __init__( self, name='FBGInterrogatorNode' ):
        super().__init__( name )
        # parameters: interrogator
        self.ip_address = self.declare_parameter( self.param_names[ 'ip' ],
                                                  '192.168.1.11' ).get_parameter_value().string_value
        self.num_samples = self.declare_parameter( self.param_names[ 'num_samples' ],
                                                   200 ).get_parameter_value().integer_value

        # fields
        self.num_chs = 4
        self.interrogator = None
        self.is_connected = self.connect()
        self.ref_wavelengths = { }

        # services
        self.reconnect_srv = self.create_service( Trigger, 'interrogator/reconnect', self.reconnect_service )
        self.calibrate_srv = self.create_service( Trigger, 'sensor/calibrate', self.ref_wl_service_old )

        # publishers
        self.connected_pub = self.create_publisher( Bool, 'interrogator/connected', 10 )
        self.signal_pubs = { }
        self.signal_pubs[ 'all' ] = {
                'raw'      : self.create_publisher( Float64MultiArray, 'sensor/raw', 10 ),
                'processed': self.create_publisher( Float64MultiArray, 'sensor/processed', 10 )
                }
        for ch in range( 1, max( 4, self.num_chs ) + 1 ):  # iterate through all of the channels to start publishers
            self.signal_pubs[ ch ] = {
                    'raw'      : self.create_publisher( Float64MultiArray, f'sensor/CH{ch}/raw', 10 ),
                    'processed': self.create_publisher( Float64MultiArray, f'sensor/CH{ch}/processed', 10 )
                    }
        # for

        self.signal_pub_timer = self.create_timer( 0.01, self.publish_peaks )

    # __init__

    def connect( self ) -> bool:
        """ Connect to interrogator (TO BE OVERRIDDEN)

            :return: True if connection is established, False if not.
        """

        self.interrogator = interrogator.Interrogator( self.ip_address, self.SM130_PORT )
        self.is_connected = self.interrogator.is_ready
        if self.is_connected:
            self.get_logger().info( "Connected to sm130 interrogator" )
            self.get_logger().info( "Connected to interrogator at: {}:{}".format( self.ip_address, self.SM130_PORT ) )

        else:
            self.get_logger().info(
                    "Did not connect to interrogator at: {}:{}".format( self.ip_address, self.SM130_PORT ) )

        # else

        return self.is_connected

    # connect

    def get_peaks( self ) -> Optional[ Dict[ int, np.ndarray ] ]:
        """ Get the demo FBG peak values

            TO BE OVERRIDDEN IN SUBCLASSES

            :returns: dict of {ch: peak numpy array} of demo peaks if connected, otherwise, None

        """
        # check if connected
        if not self.is_connected or not self.interrogator.is_ready:
            return None

        peak_msg = self.interrogator.getData()
        peak_msg = interrogator.PeakMessage()

        peak_dict = {
                1: np.array( peak_msg.peak_container.peaks.CH1, dtype=np.float64 ),
                2: np.array( peak_msg.peak_container.peaks.CH2, dtype=np.float64 ),
                3: np.array( peak_msg.peak_container.peaks.CH3, dtype=np.float64 ),
                4: np.array( peak_msg.peak_container.peaks.CH4, dtype=np.float64 )
                }

        return peak_dict

    # get_peaks

    def parse_peaks( self, peak_data: dict ) -> Optional[ Dict[ int, np.ndarray ] ]:
        """ Parse the peak data into a dict"""

        if peak_data is None:
            return None

        data = { }
        for ch in range( 1, 4 + 1 ):
            data[ ch ] = peak_data[ ch ].astype( np.float64 )

        # for

        return data

    # parse_peaks

    @staticmethod
    def parsed_peaks_to_msg( parsed_peaks ) -> (Float64MultiArray, Dict[int, Float64MultiArray]):
        """ Convert the parsed peaks to a Float64MultiArray msgs (total and per channel)"""
        # initialize the Total message
        total_msg = Float64MultiArray()
        total_msg.layout.dim = [ ]
        total_msg.data = [ ]

        channel_msgs = { }

        for ch_num, peaks in parsed_peaks.items():
            # prepare the individual channel msg
            ch_msg = Float64MultiArray()
            ch_msg.layout.dim.append( MultiArrayDimension( label=f"CH{ch_num}",
                                                           stride=peaks.dtype.itemsize,
                                                           size=peaks.size * peaks.dtype.itemsize ) )
            ch_msg.data = peaks.flatten().tolist()

            channel_msgs[ ch_num ] = ch_msg

            # append to total msg
            total_msg.layout.dim.append( ch_msg.layout.dim[ 0 ] )
            total_msg.data += ch_msg.data

        # for

        return total_msg, channel_msgs

    # parsed_peaks_to_msg

    def process_signals( self, peak_signals: dict ) -> dict:
        """ Method to perform the signal processing

        ( copied from hyperion_talker.py file from ros2_hyperion_interrogator github repo )

            This includes:
                - Base wavelength shifting

        """
        proc_signals = { }

        for ch_num, peaks in peak_signals.items():
            proc_signals[ ch_num ] = { }

            proc_signals[ ch_num ][ 'raw' ] = peaks
            try:
                proc_signals[ ch_num ][ 'processed' ] = peaks - self.ref_wavelengths[ ch_num ]

            # try

            except KeyError as e:
                continue

            # except

        # for

        return proc_signals

    # process_signals

    def publish_peaks( self ):
        """ Publish the peaks on an timer
            ( copied from hyperion_talker.py file from ros2_hyperion_interrogator github repo )

            Uses fields:
                self.is_connected (bool) for publishing connection status of interrogator

        """
        # publish the connection status
        self.connected_pub.publish( Bool( data=self.is_connected ) )

        # check if interrogator is connected
        if not self.is_connected or not self.interrogator.is_ready:
            return

        # if

        peaks = self.parse_peaks( self.get_peaks() )
        if peaks is None:  # check for empty peak parsing
            self.get_logger().warning( f"Cannot publish peaks! Peaks value is {peaks}" )
            return
        # if

        all_peaks = self.process_signals( peaks )  # perform processing

        # split the peaks into raw and processed signals
        raw_peaks = dict( (ch_num, all_peaks[ ch_num ][ 'raw' ]) for ch_num in all_peaks.keys() )

        proc_ch_nums = [ ch for ch in all_peaks.keys() if 'processed' in all_peaks[ ch ].keys() ]
        proc_peaks = dict( (ch, all_peaks[ ch ][ 'processed' ]) for ch in proc_ch_nums )

        # prepare messages
        raw_tot_msg, raw_ch_msgs = self.parsed_peaks_to_msg( raw_peaks )
        proc_tot_msg, proc_ch_msgs = self.parsed_peaks_to_msg( proc_peaks )

        for ch_num in all_peaks.keys():
            # raw signals
            raw_pub = self.signal_pubs[ ch_num ][ 'raw' ]
            raw_msg = raw_ch_msgs[ ch_num ]
            raw_pub.publish( raw_msg )

            # processed signals
            if ch_num in proc_peaks.keys():
                proc_msg = proc_ch_msgs[ ch_num ]
                proc_pub = self.signal_pubs[ ch_num ][ 'processed' ]
                proc_pub.publish( proc_msg )

            # if

        # ch_num

        # publish the entire signal
        self.signal_pubs[ 'all' ][ 'raw' ].publish( raw_tot_msg )
        self.get_logger().debug( "Published raw peak values: {}".format( raw_tot_msg.data ) )

        if len( proc_tot_msg.data ) > 0:
            self.signal_pubs[ 'all' ][ 'processed' ].publish( proc_tot_msg )
            self.get_logger().debug( "Published processed peak values: {}".format( proc_tot_msg.data ) )

        # if

    # publish_peaks

    def reconnect_service( self, request: Trigger.Request, response: Trigger.Response ):
        """ reconnect to the IP address """
        self.get_logger().info( "Reconnecting to Hyperion interrogator..." )
        self.connect()

        response.success = self.is_connected

        if not self.is_connected:
            response.message = "{} is not a valid IP for the Hyperion interrogator".format( self.ip_address )

        # if

        return response

    # reconnect_service

    def ref_wl_service( self, request: Trigger.Request, response: Trigger.Response ):
        """ Service to get the reference wavelength OVERRIDE

            Runs child node in parallel

            TODO:
                - include timeout (5 seconds)
                - make functional (Not working with non-async publishing

        """
        raise NotImplementedError( "ref_wl_service is not yet functional for this node." )
        self.get_logger().info( f"Starting to recalibrate the sensors wavelengths for {self.num_samples} samples." )

        # initialize data container
        data = { }
        counter = 0

        def update_data( msg ):
            nonlocal data, counter

            # parse in FBG msgs
            peaks = self.unpack_fbg_msg( msg )
            self.get_logger().info( f"Sensor Calibration: Counter = {counter}, calibration (Agg.) | Peaks = {peaks}" )
            self.get_logger().info( f"Sensor Calibration: Data = {data}" )
            for ch_num, ch_peaks in peaks.items():
                if ch_num not in data.keys():
                    data[ ch_num ] = ch_peaks

                else:
                    data[ ch_num ] += ch_peaks

            # for

            # increment counter
            counter += 1

        # update_data

        # temporary subscriber node to raw data
        tmp_node = Node( 'TempSignalSubscriber' )
        tmp_sub = tmp_node.create_subscription( Float64MultiArray, self.signal_pubs[ 'all' ][ 'raw' ].topic_name,
                                                update_data, 10 )
        self.get_logger().info( "Created temporary subscriber" )

        # Wait to gather 200 signals
        while counter < self.num_samples:
            rclpy.spin_once( tmp_node )

        # while

        # normalize the data
        for ch_num, agg_peaks in data.items():
            self.ref_wavelengths[ ch_num ] = agg_peaks / counter

        # for

        response.success = True
        self.get_logger().info( "Recalibration successful" )
        for ch, peaks in self.ref_wavelengths.items():
            self.get_logger().info( f"Reference wavelengths for CH{ch}: {peaks}" )

        # destroy the subscriber
        if tmp_node.handle:
            tmp_node.destroy_node()

        # if

        return response

    # ref_wl_service

    def ref_wl_service_old( self, request: Trigger.Request, response: Trigger.Response ):
        """ Service to get the reference wavelength/calibrate the signals

            Old implementation, causes for node to stop publishing

        """
        if self.is_connected:
            self.get_logger().info( f"Starting to recalibrate the sensors wavelengths for {self.num_samples} samples." )

            data = { }
            counter = 0
            error_counter = 0
            max_errors = 5

            while counter < self.num_samples:
                try:
                    signal = self.parse_peaks( self.get_peaks() )

                    for ch_num, peaks in signal.items():
                        if ch_num not in data.keys():
                            data[ ch_num ] = peaks

                        else:
                            data[ ch_num ] += peaks

                    # for

                    # increment counter
                    counter += 1
                    error_counter = 0

                # try
                except:  # no peaks
                    error_counter += 1

                    if error_counter > max_errors:  # make sure we don't go into an endless loop
                        response.success = False
                        response.message = "Timeout interrogation occurred."
                        return response

                    # if
                    continue

                # except
            # while

            # find the average
            for ch_num, agg_peaks in data.items():
                self.ref_wavelengths[ ch_num ] = agg_peaks / self.num_samples
            # for

            response.success = True
            self.get_logger().info( "Recalibration successful" )
            for ch, peaks in self.ref_wavelengths.items():
                self.get_logger().info( f"Reference wavelengths for CH{ch}: {peaks}" )

        # if
        else:
            response.success = False
            response.message = "Interrogator was not able to gather peaks. Check connection and IP address."

        # else

        return response

    # ref_wl_service_old

    @staticmethod
    def unpack_fbg_msg( msg ) -> dict:
        """ Unpack Float64MultiArray into dict of numpy arrays """
        ret_val = { }
        idx_i = 0

        for dim in msg.layout.dim:
            ch_num = int( dim.label.strip( 'CH' ) )
            size = int( dim.size / dim.stride )

            ret_val[ ch_num ] = np.float64( msg.data[ idx_i:idx_i + size ] )

            idx_i += size  # increment size to next counter

        # for

        return ret_val

    # unpack_fbg_msg


# class: FBGInterrogatorNode

def main(args = None):
    rclpy.init(args=args)

    node = FBGInterrogatorNode()

    rclpy.spin(node)

    rclpy.shutdown()

# main


if __name__ == '__main__':
    main()

# if __main__
