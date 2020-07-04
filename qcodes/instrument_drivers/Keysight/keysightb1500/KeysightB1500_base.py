import re
import textwrap
from typing import Optional, Union, Dict, List, Tuple
from collections import defaultdict

from qcodes import VisaInstrument, MultiParameter
from qcodes.instrument_drivers.Keysight.keysightb1500.KeysightB1500_module \
    import _FMTResponse, fmt_response_base_parser, StatusMixin
from qcodes.utils.helpers import create_on_off_val_mapping
from .KeysightB1530A import B1530A
from .KeysightB1520A import B1520A
from .KeysightB1517A import B1517A
from .KeysightB1500_module import B1500Module, parse_module_query_response, \
    parse_spot_measurement_response
from . import constants
from .message_builder import MessageBuilder


class KeysightB1500(VisaInstrument):
    """Driver for Keysight B1500 Semiconductor Parameter Analyzer.

    For the list of supported modules, refer to :meth:`from_model_name`.
    """
    calibration_time_out = 60  # 30 seconds suggested by manual
    def __init__(self, name, address, **kwargs):
        super().__init__(name, address, terminator="\r\n", **kwargs)
        self.by_slot = {}
        self.by_channel = {}
        self.by_kind = defaultdict(list)

        self._find_modules()

        self.add_parameter('autozero_enabled',
                           unit='',
                           label='Autozero enabled of the high-resolution ADC',
                           set_cmd=self._set_autozero,
                           get_cmd=None,
                           val_mapping=create_on_off_val_mapping(
                               on_val=True, off_val=False),
                           initial_cache_value=False,
                           docstring=textwrap.dedent("""
            Enable or disable cancelling of the offset of the 
            high-resolution A/D converter (ADC).
    
            Set the function to OFF in cases that the measurement speed is 
            more important than the measurement accuracy. This roughly halves
            the integration time."""))

        self.add_parameter(name='run_iv_staircase_sweep',
                           parameter_class=IVSweepMeasurement,
                           docstring=textwrap.dedent("""
               This is MultiParameter. Running the sweep runs the measurement 
               on the list of source values defined using 
               `setup_staircase_sweep` method. The output is a 
               primary parameter (e.g. Gate current)  and a secondary  
               parameter (e.g. Source/Drain current) both of which use the same 
               setpoints. Note you must `set_measurement_mode` and specify 
               2 channels as the argument before running the sweep. First 
               channel (SMU) must be the channel on which you set the sweep (
               WV) and second channel(SMU) must be the one which remains at 
               constants voltage. 
                              """))

        self.connect_message()

    def write(self, cmd):
        """
        Extend write method from the super to ask for error message each
        time a write command is called.
        """
        super().write(cmd)
        error_message = self.error_message()
        if error_message != '+0,"No Error."':
            raise RuntimeError(f"While setting this parameter received "
                               f"error: {error_message}")

    def add_module(self, name: str, module: B1500Module) -> None:
        super().add_submodule(name, module)

        self.by_kind[module.MODULE_KIND].append(module)
        self.by_slot[module.slot_nr] = module
        for ch in module.channels:
            self.by_channel[ch] = module

    def reset(self) -> None:
        """Performs an instrument reset.

        This does not reset error queue!
        """
        self.write("*RST")

    def get_status(self) -> int:
        return int(self.ask("*STB?"))

    # TODO: Data Output parser: At least for Format FMT1,0 and maybe for a
    # second (binary) format. 8 byte binary format would be nice because it
    # comes with time stamp
    # FMT1,0: ASCII (12 digits data with header) <CR/LF^EOI>

    def _find_modules(self) -> None:
        from .constants import UNT

        r = self.ask(MessageBuilder()
                     .unt_query(mode=UNT.Mode.MODULE_INFO_ONLY)
                     .message
                     )

        slot_population = parse_module_query_response(r)

        for slot_nr, model in slot_population.items():
            module = self.from_model_name(model, slot_nr, self)

            self.add_module(name=module.short_name, module=module)

    @staticmethod
    def from_model_name(model: str, slot_nr: int, parent: 'KeysightB1500',
                        name: Optional[str] = None) -> 'B1500Module':
        """Creates the correct instance of instrument module by model name.

        Args:
            model: Model name such as 'B1517A'
            slot_nr: Slot number of this module (not channel number)
            parent: Reference to B1500 mainframe instance
            name: Name of the instrument instance to create. If `None`
                (Default), then the name is autogenerated from the instrument
                class.

        Returns:
            A specific instance of :class:`.B1500Module`
        """
        if model == "B1517A":
            return B1517A(slot_nr=slot_nr, parent=parent, name=name)
        elif model == "B1520A":
            return B1520A(slot_nr=slot_nr, parent=parent, name=name)
        elif model == "B1530A":
            return B1530A(slot_nr=slot_nr, parent=parent, name=name)
        else:
            raise NotImplementedError("Module type not yet supported.")

    def enable_channels(self, channels: Optional[constants.ChannelList] = None
                        ) -> None:
        """Enable specified channels.

        If channels is omitted or `None`, then all channels are enabled.
        """
        msg = MessageBuilder().cn(channels)

        self.write(msg.message)

    def disable_channels(
            self,
            channels: Optional[constants.ChannelList] = None
    ) -> None:
        """Disable specified channels.

        If channels is omitted or `None`, then all channels are disabled.
        """
        msg = MessageBuilder().cl(channels)

        self.write(msg.message)

    # Response parsing functions as static methods for user convenience
    parse_spot_measurement_response = parse_spot_measurement_response
    parse_module_query_response = parse_module_query_response

    def _setup_integration_time(self,
                                adc_type: constants.AIT.Type,
                                mode: Union[constants.AIT.Mode, int],
                                coeff: Optional[int] = None
                                ) -> None:
        """See :meth:`MessageBuilder.ait` for information"""
        if coeff is not None:
            coeff = int(coeff)
        self.write(MessageBuilder()
                   .ait(adc_type=adc_type, mode=mode, coeff=coeff)
                   .message
                   )

    def _reset_measurement_statuses_of_smu_spot_measurement_parameters(
            self, parameter_name: str) -> None:
        if parameter_name not in ('voltage', 'current'):
            raise ValueError(f'Parameter name should be one of [voltage,current], '
                             f'got {parameter_name}.')
        for smu in self.by_kind[constants.ModuleKind.SMU]:
            smu.parameters[parameter_name]._measurement_status = None

    def use_nplc_for_high_speed_adc(
            self, n: Optional[int] = None) -> None:
        """
        Set the high-speed ADC to NPLC mode, with optionally defining number
        of averaging samples via argument `n`.

        Args:
            n: Value that defines the number of averaging samples given by
                the following formula:

                ``Number of averaging samples = n / 128``.

                n=1 to 100. Default setting is 1 (if `None` is passed).

                The Keysight B1500 gets 128 samples in a power line cycle,
                repeats this for the times you specify, and performs
                averaging to get the measurement data. (For more info see
                Table 4-21.).  Note that the integration time will not be
                updated if a non-integer value is written to the B1500.
        """
        self._setup_integration_time(
            adc_type=constants.AIT.Type.HIGH_SPEED,
            mode=constants.AIT.Mode.NPLC,
            coeff=n
        )

    def use_nplc_for_high_resolution_adc(
            self, n: Optional[int] = None) -> None:
        """
        Set the high-resolution ADC to NPLC mode, with optionally defining
        the number of PLCs per sample via argument `n`.

        Args:
            n: Value that defines the integration time given by the
                following formula:

                ``Integration time = n / power line frequency``.

                n=1 to 100. Default setting is 1 (if `None` is passed).
                (For more info see Table 4-21.).  Note that the integration
                time will not be updated if a non-integer value is written
                to the B1500.
        """
        self._setup_integration_time(
            adc_type=constants.AIT.Type.HIGH_RESOLUTION,
            mode=constants.AIT.Mode.NPLC,
            coeff=n
        )

    def use_manual_mode_for_high_speed_adc(
            self, n: Optional[int] = None) -> None:
        """
        Set the high-speed ADC to manual mode, with optionally defining number
        of averaging samples via argument `n`.

        Use ``n=1`` to disable averaging (``n=None`` uses the default
        setting from the instrument which is also ``n=1``).

        Args:
            n: Number of averaging samples, between 1 and 1023. Default
                setting is 1. (For more info see Table 4-21.)
                Note that the integration time will not be updated
                if a non-integer value is written to the B1500.
        """
        self._setup_integration_time(
            adc_type=constants.AIT.Type.HIGH_SPEED,
            mode=constants.AIT.Mode.MANUAL,
            coeff=n
        )

    def _set_autozero(self, do_autozero: bool) -> None:
        self.write(MessageBuilder().az(do_autozero=do_autozero).message)

    def self_calibration(self,
                         slot: Optional[Union[constants.SlotNr, int]] = None
                         ) -> constants.CALResponse:
        """
        Performs the self calibration of the specified module (SMU) and
        returns the result. Failed modules are disabled, and can only be
        enabled by the ``RCV`` command.

        Calibration takes about 30 seconds (the visa timeout for it is
        controlled by :attr:`calibration_time_out` attribute).

        Execution Conditions: No SMU may be in the high voltage state
        (forcing more than ±42 V, or voltage compliance set to more than
        ±42 V). Before starting the calibration, open the measurement
        terminals.

        Args:
            slot: Slot number of the slot that installs the module to perform
                the self-calibration. For Ex:
                constants.SlotNr.ALL, MAINFRAME, SLOT01, SLOT02 ...SLOT10
                If not specified, the calibration is performed for all the
                modules and the mainframe.
        """
        msg = MessageBuilder().cal_query(slot=slot)
        with self.root_instrument.timeout.set_to(self.calibration_time_out):
            response = self.ask(msg.message)
        return constants.CALResponse(int(response))

    def error_message(self, mode: Optional[Union[constants.ERRX.Mode,
                                                 int]] = None) -> str:
        """
        This method reads one error code from the head of the error
        queue and removes that code from the queue. The read error is
        returned as the response of this method.

        Args:
            mode: If no valued passed returns both the error value and the
                error message. See :class:`.constants.ERRX.Mode` for possible
                arguments.

        Returns:
            In the default case response message contains an error message
            and a custom message containing additional information such as
            the slot number. They are separated by a semicolon (;). For
            example, if the error 305 occurs on the slot 1, this method
            returns the following response. 305,"Excess current in HPSMU.;
            SLOT1" If no error occurred, this command returns 0,"No Error."
        """

        msg = MessageBuilder().errx_query(mode=mode)
        response = self.ask(msg.message)
        return response

    def clear_buffer_of_error_message(self) -> None:
        """
        This method clears the error message stored in buffer when the
        error_message command is executed.
        """
        msg = MessageBuilder().err_query()
        self.write(msg.message)

    def clear_timer_count(self, chnum: Optional[int] = None) -> None:
        """
        This command clears the timer count. This command is effective for
        all measurement modes, regardless of the TSC setting. This command
        is not effective for the 4 byte binary data output format
        (FMT3 and FMT4).

        Args:
            chnum: SMU or MFCMU channel number. Integer expression. 1 to 10.
                See Table 4-1 on page 16 of 2016 manual. If chnum is
                specified, this command clears the timer count once at the
                source output start by the DV, DI, or DCV command for the
                specified channel. The channel output switch of the
                specified channel must be ON when the timer count is
                cleared.

        If chnum is not specified, this command clears the timer count
        immediately,
        """
        msg = MessageBuilder().tsr(chnum=chnum)
        self.write(msg.message)

    def set_measurement_mode(self,
                             mode: Union[constants.MM.Mode, int],
                             channels: Optional[constants.ChannelList] = None
                             ) -> None:
        """
        This method specifies the measurement mode and the channels used
        for measurements. This method must be entered to specify the
        measurement mode. For the high speed spot measurements,
        do not use this method.
        NOTE Order of the channels are important. The SMU which is setup to
        run the sweep goes first.

        Args:
            mode: Measurement mode. See `constants.MM.Mode` for all possible
                modes
            channels: Measurement channel number. See `constants.ChannelList`
                for all possible channels.
        """
        msg = MessageBuilder().mm(mode=mode, channels=channels).message
        self.write(msg)

    def get_measurement_mode(self) -> Dict[str, Union[constants.MM.Mode,
                                                      List]]:
        """
        This method gets the measurement mode(MM) and the channels used
        for measurements. It outputs a dictionary with 'mode' and
        'channels' as keys.
        """
        msg = MessageBuilder().lrn_query(type_id=constants.LRN.
                                         Type.TM_AV_CM_FMT_MM_SETTINGS)
        response = self.ask(msg.message)
        match = re.search('MM(?P<mode>.*?),(?P<channels>.*?)(;|$)', response)

        if not match:
            raise ValueError('Measurement Mode (MM) not found.')

        out_dict: Dict[str, Union[constants.MM.Mode, List]] = {}
        resp_dict = match.groupdict()
        out_dict['mode'] = constants.MM.Mode(int(resp_dict['mode']))
        out_dict['channels'] = list(map(int, resp_dict['channels'].split(',')))
        return out_dict

    def get_response_format_and_mode(self) -> \
            Dict[str, Union[constants.FMT.Format, constants.FMT.Mode]]:
        """
        This method queries the the data output format and mode.
        """
        msg = MessageBuilder().lrn_query(type_id=constants.LRN.
                                         Type.TM_AV_CM_FMT_MM_SETTINGS)
        response = self.ask(msg.message)
        match = re.search('FMT(?P<format>.*?),(?P<mode>.*?)(;|$)',
                          response)

        if not match:
            raise ValueError('Measurement Mode (FMT) not found.')

        out_dict: Dict[str, Union[constants.FMT.Format, constants.FMT.Mode]] \
            = {}
        resp_dict = match.groupdict()
        out_dict['format'] = constants.FMT.Format(int(resp_dict[
                                                          'format']))
        out_dict['mode'] = constants.FMT.Mode(int(resp_dict['mode']))
        return out_dict

    def enable_smu_filters(
            self,
            enable_filter: bool,
            channels: Optional[constants.ChannelList] = None
    ) -> None:
        """
        This methods sets the connection mode of a SMU filter for each channel.
        A filter is mounted on the SMU. It assures clean source output with
        no spikes or overshooting. A maximum of ten channels can be set.

        Args:
            enable_filter : Status of the filter.
                False: Disconnect (initial setting).
                True: Connect.
            channels : SMU channel number. Specify channel from
                `constants.ChNr` If you do not specify chnum,  the FL
                command sets the same mode for all channels.
        """
        self.write(MessageBuilder().fl(enable_filter=enable_filter,
                                       channels=channels).message)


class IVSweepMeasurement(MultiParameter, StatusMixin):
    """
    IV sweep measurement outputs a list of primary and secondary
    parameter.

    Args:
        name: Name of the Parameter.
        instrument: Instrument to which this parameter communicates to.
    """

    def __init__(self, name: str, instrument: B1517A, **kwargs):
        super().__init__(
            name,
            names=tuple(['param1', 'param2']),
            units=tuple(['A', 'A']),
            labels=tuple(['Param1 Current', 'Param2 Current']),
            shapes=((1,),) * 2,
            setpoint_names=(('Voltage',),) * 2,
            setpoint_labels=(('Voltage',),) * 2,
            setpoint_units=(('V',),) * 2,
            instrument=instrument,
            **kwargs)

        self.instrument: B1517A
        self.root_instrument: KeysightB1500

        self.param1 = _FMTResponse(None, None, None, None)
        self.param2 = _FMTResponse(None, None, None, None)
        self.source_voltage = _FMTResponse(None, None, None, None)
        self._fudge: float = 1.5

    def get_raw(self) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
        measurement_mode = self.instrument.get_measurement_mode()
        if len(measurement_mode['channels']) != 2:
            raise ValueError('Two measurement channels are needed, one for '
                             'gate current and other for source drain '
                             'current.')

        smu = self.instrument.by_channel[measurement_mode['channels'][0]]

        if not smu.setup_fnc_already_run:
            raise Exception(f'Sweep setup has not yet been run successfully '
                            f'on {smu.full_name}')

        delay_time = smu.iv_sweep.step_delay()
        if smu._average_coefficient < 0:
            # negative coefficient means nplc and positive means just
            # averaging
            nplc = 128 * abs(smu._average_coefficient)
            power_line_time_period = 1 / smu.power_line_frequency
            calculated_time = 2 * nplc * power_line_time_period
        else:
            calculated_time = smu._average_coefficient * \
                              delay_time
        num_steps = smu.iv_sweep.sweep_steps()
        estimated_timeout = max(delay_time, calculated_time) * num_steps
        new_timeout = estimated_timeout * self._fudge

        format_and_mode = self.instrument.get_response_format_and_mode()
        fmt_format = format_and_mode['format']
        fmt_mode = format_and_mode['mode']
        try:
            self.root_instrument.write(MessageBuilder().fmt(1, 1).message)
            with self.root_instrument.timeout.set_to(new_timeout):
                raw_data = self.instrument.ask(MessageBuilder().xe().message)
                parsed_data = fmt_response_base_parser(raw_data)
        finally:
            self.root_instrument.write(MessageBuilder().fmt(fmt_format,
                                                            fmt_mode).message)

        self.param1 = _FMTResponse(
            *[parsed_data[i][::3] for i in range(0, 4)])
        self.param2 = _FMTResponse(
            *[parsed_data[i][1::3] for i in range(0, 4)])
        self.source_voltage = _FMTResponse(
            *[parsed_data[i][2::3] for i in range(0, 4)])

        self.shapes = ((len(self.source_voltage.value),),) * 2
        self.setpoints = ((self.source_voltage.value,),) * 2

        return self.param1.value, self.param2.value